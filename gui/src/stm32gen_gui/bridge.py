"""表单数据 ↔ 配置模型的桥接层。

**本模块不依赖 Qt**，因此可以完整单测。这是刻意的设计：GUI 里最容易出错的
不是控件摆放，而是「表格里的一行 ↔ 模型里的一条配置」的双向转换 ——
把这段逻辑隔离出来，就能在没有显示器、没有事件循环的环境下验证它。

为什么需要中间层
----------------
Qt 的表格控件存的是字符串（用户在单元格里输入的字面文本），
而模型要的是枚举与整数。直接让控件持有模型会导致：

- 用户输入 ``"PA9"`` 时不知道该怎么解析（端口与引脚混在一起）
- 输入非法时控件报错时机难控

所以约定：**控件只存原始文本，转换集中在本模块**，转换失败返回结构化的
错误信息而不是抛异常 —— GUI 需要把这些错误显示在对应单元格上。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import ValidationError
from stm32gen.models import (
    AfioConfig,
    ChipId,
    ExtiLineConfig,
    GpioInitLevel,
    GpioMode,
    GpioPinConfig,
    GpioPort,
    GpioSpeed,
    NvicChannelConfig,
    NvicConfig,
    NvicPriorityGroup,
    Stm32Config,
    TimChannelConfig,
    TimConfig,
    UsartConfig,
    UsartDirection,
    UsartFlowControl,
    UsartInstance,
    UsartInterrupt,
    UsartParity,
    UsartRemap,
    UsartStopBits,
    UsartWordLength,
    get_chip,
)
from stm32gen.models.enums import (
    EXTI_EXTERNAL_LINE_MAX,
    EXTI_EXTERNAL_LINE_MIN,
    AfioSwjRemap,
    ExtiMode,
    ExtiTrigger,
    TimChannel,
    TimClockDivision,
    TimClockSource,
    TimCounterMode,
    TimExtTriggerPolarity,
    TimExtTriggerPrescaler,
    TimInstance,
    TimInterrupt,
    TimOcMode,
    TimOcPolarity,
    TimOcPreload,
    TimOutputState,
    TimRemap,
)
from stm32gen.models.tim import (
    TIM_EXT_FILTER_MAX,
    TIM_REGISTER_MAX,
    TimTimingSolution,
    solve_period,
    solve_timing,
)

# 调试口引脚表复用校验层的那一份，避免两处各有一份而漂移：
# 若 GUI 认为某引脚可用、校验层却报 pin.debug_port 警告，用户会失去信任。
# 导入的是纯数据常量，不引入 Qt 依赖（本模块必须保持可无头单测）。
from stm32gen.validate.rules import DEBUG_PINS


@dataclass
class FieldError:
    """一条字段级转换错误，供 GUI 标红对应单元格。"""

    field: str
    message: str

    def format(self) -> str:
        return f"{self.field}: {self.message}"


@dataclass
class ConvertResult:
    """转换结果：成功时带模型，失败时带错误列表。"""

    value: object | None = None
    errors: list[FieldError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.value is not None and not self.errors


# --------------------------------------------------------------------------
# 枚举选项（供下拉框填充）
# --------------------------------------------------------------------------


def mode_options() -> list[tuple[str, str]]:
    """模式下拉选项，显示名带「输入/输出」分类便于选择。"""
    result: list[tuple[str, str]] = []
    for item in GpioMode:
        kind = "输入" if item.is_input else "输出"
        if item.is_alternate:
            kind = "复用"
        result.append((item.value, f"{item.name}（{kind}）"))
    return result


def speed_options() -> list[tuple[str, str]]:
    return [(item.value, item.name.replace("SPEED_", "")) for item in GpioSpeed]


def init_level_options() -> list[tuple[str, str]]:
    return [(item.value, "低电平" if item is GpioInitLevel.LOW else "高电平")
            for item in GpioInitLevel]


def usart_instance_options() -> list[tuple[str, str]]:
    return [(item.value, item.value) for item in UsartInstance]


def word_length_options() -> list[tuple[str, str]]:
    return [(item.value, item.name[1:] + " 位") for item in UsartWordLength]


def stop_bits_options() -> list[tuple[str, str]]:
    return [(item.value, item.name[1:].replace("_", ".")) for item in UsartStopBits]


def parity_options() -> list[tuple[str, str]]:
    labels = {
        UsartParity.NONE: "无校验",
        UsartParity.EVEN: "偶校验 (Even)",
        UsartParity.ODD: "奇校验 (Odd)",
    }
    return [(item.value, labels[item]) for item in UsartParity]


def direction_options() -> list[tuple[str, str]]:
    labels = {
        UsartDirection.RX: "只接收",
        UsartDirection.TX: "只发送",
        UsartDirection.RX_TX: "收发",
    }
    return [(item.value, labels[item]) for item in UsartDirection]


def flow_control_options() -> list[tuple[str, str]]:
    labels = {
        UsartFlowControl.NONE: "无",
        UsartFlowControl.RTS: "RTS",
        UsartFlowControl.CTS: "CTS",
        UsartFlowControl.RTS_CTS: "RTS + CTS",
    }
    return [(item.value, labels[item]) for item in UsartFlowControl]


def interrupt_options() -> list[tuple[str, str]]:
    labels = {
        UsartInterrupt.RXNE: "RXNE（收到数据）",
        UsartInterrupt.IDLE: "IDLE（总线空闲）",
        UsartInterrupt.TXE: "TXE（发送寄存器空）",
        UsartInterrupt.TC: "TC（发送完成）",
        UsartInterrupt.PE: "PE（校验错误）",
    }
    return [(item.value, labels[item]) for item in UsartInterrupt]


def remap_options() -> list[tuple[str, str]]:
    labels = {
        UsartRemap.NONE: "不重映射（默认引脚）",
        UsartRemap.USART1: "USART1 → PB6/PB7",
        UsartRemap.USART2: "USART2 → PD5/PD6",
        UsartRemap.USART3_PARTIAL: "USART3 → PC10/PC11",
        UsartRemap.USART3_FULL: "USART3 → PD8/PD9",
    }
    return [(item.value, labels[item]) for item in UsartRemap]


def priority_group_options() -> list[tuple[str, str]]:
    """分组选项带上位宽说明，用户不用记 0~4 各是什么。"""
    return [
        (
            item.value,
            f"{item.value}（抢占 {item.preempt_bits} 位 0~{item.max_preempt}"
            f" / 子 {item.sub_bits} 位 0~{item.max_sub}）",
        )
        for item in NvicPriorityGroup
    ]


def chip_options() -> list[tuple[str, str]]:
    return [(item.value, item.value) for item in ChipId]


# --------------------------------------------------------------------------
# EXTI / TIM / AFIO 的选项
# --------------------------------------------------------------------------


def exti_mode_options() -> list[tuple[str, str]]:
    """EXTI 工作模式。"""
    labels = {
        ExtiMode.INTERRUPT: "中断（需要 NVIC 通道与 ISR）",
        ExtiMode.EVENT: "事件（不产生中断，不需要 NVIC）",
    }
    return [(item.value, labels[item]) for item in ExtiMode]


def exti_trigger_options() -> list[tuple[str, str]]:
    """EXTI 触发边沿。"""
    return [(item.value, item.label) for item in ExtiTrigger]


def afio_swj_options() -> list[tuple[str, str]]:
    """SWJ 调试口释放选项。

    标签里直接写清释放哪些引脚、以及会不会关掉 SWD ——
    后者是**不可逆风险**（关掉 SWD 后调试器再也连不上），
    必须在选的那一刻就看见，而不是等校验层报错。
    """
    labels: dict[AfioSwjRemap, str] = {
        AfioSwjRemap.NONE: "不释放（保留 SWD + JTAG，最安全）",
        AfioSwjRemap.JTAG_DISABLE: "只关 JTAG，保留 SWD（释放 PA15/PB3/PB4）",
        AfioSwjRemap.SWJ_DISABLE: "关闭 SWD + JTAG（释放 5 个引脚，危险：无法再下载）",
        AfioSwjRemap.NO_JTRST: "只关 NJTRST（释放 PB4）",
    }
    return [(item.value, labels[item]) for item in AfioSwjRemap]


def tim_instance_options() -> list[tuple[str, str]]:
    """可用的 TIM 实例，标签注明高级/通用与总线。"""
    result: list[tuple[str, str]] = []
    for item in TimInstance:
        kind = "高级" if item.is_advanced else "通用"
        bus = "APB2" if item.is_apb2 else "APB1"
        result.append((item.value, f"{item.value}（{kind}，{bus}）"))
    return result


def tim_counter_mode_options() -> list[tuple[str, str]]:
    labels = {
        TimCounterMode.UP: "向上计数",
        TimCounterMode.DOWN: "向下计数",
        TimCounterMode.CENTER_ALIGNED1: "中央对齐 1（更新频率减半）",
        TimCounterMode.CENTER_ALIGNED2: "中央对齐 2（更新频率减半）",
        TimCounterMode.CENTER_ALIGNED3: "中央对齐 3（更新频率减半）",
    }
    return [(item.value, labels[item]) for item in TimCounterMode]


def tim_clock_division_options() -> list[tuple[str, str]]:
    """CKD 选项。

    标签特意写明"不影响计数频率"—— 这是最常见的误解：
    想改周期却去调 CKD，结果计数频率纹丝不动。
    """
    return [
        (item.value, f"{item.name.replace('DIV', '÷')}（只影响滤波器/死区，不改计数频率）")
        for item in TimClockDivision
    ]


def tim_oc_mode_options() -> list[tuple[str, str]]:
    labels = {
        TimOcMode.TIMING: "Timing（仅计时，不改引脚）",
        TimOcMode.ACTIVE: "Active（匹配时置有效）",
        TimOcMode.INACTIVE: "Inactive（匹配时置无效）",
        TimOcMode.TOGGLE: "Toggle（匹配时翻转）",
        TimOcMode.PWM1: "PWM1（计数值 < CCR 时有效）",
        TimOcMode.PWM2: "PWM2（计数值 < CCR 时无效）",
    }
    return [(item.value, labels[item]) for item in TimOcMode]


def tim_channel_options() -> list[tuple[str, str]]:
    return [(item.value, item.value.replace("TIM_Channel_", "通道 ")) for item in TimChannel]


def tim_polarity_options() -> list[tuple[str, str]]:
    return [
        (item.value, "高电平有效" if item is TimOcPolarity.HIGH else "低电平有效")
        for item in TimOcPolarity
    ]


def tim_preload_options() -> list[tuple[str, str]]:
    """预装载选项。PWM 场景必须开，标签里说明原因。"""
    return [
        (
            item.value,
            "使能（PWM 必须开：改 CCR 时不会产生残缺脉冲）"
            if item is TimOcPreload.ENABLE
            else "禁用（改 CCR 立即生效，可能产生毛刺）",
        )
        for item in TimOcPreload
    ]


def tim_output_state_options() -> list[tuple[str, str]]:
    return [
        (item.value, "使能" if item is TimOutputState.ENABLE else "禁用")
        for item in TimOutputState
    ]


def tim_interrupt_options() -> list[tuple[str, str]]:
    labels = {
        TimInterrupt.UPDATE: "Update（更新/溢出）",
        TimInterrupt.CC1: "CC1（通道 1 捕获比较）",
        TimInterrupt.CC2: "CC2（通道 2 捕获比较）",
        TimInterrupt.CC3: "CC3（通道 3 捕获比较）",
        TimInterrupt.CC4: "CC4（通道 4 捕获比较）",
        TimInterrupt.TRIGGER: "Trigger（触发，仅高级定时器）",
        TimInterrupt.BREAK: "Break（刹车，仅高级定时器）",
    }
    return [(item.value, labels[item]) for item in TimInterrupt]


def tim_remap_options(instance: str | None = None) -> list[tuple[str, str]]:
    """TIM 重映射选项；给定实例时只列该实例可用的。

    **宏名不规则**（TIM2/3 用 PartialRemap/FullRemap，TIM4 是
    ``GPIO_Remap_TIM4``），所以标签直接显示宏名而不是猜一个漂亮的写法。
    """
    if instance:
        try:
            target = TimInstance(instance)
        except ValueError:
            target = None
        options = TimRemap.options_for(target) if target else tuple(TimRemap)
    else:
        options = tuple(TimRemap)

    result: list[tuple[str, str]] = []
    for item in options:
        if item is TimRemap.NONE:
            result.append((item.value, "不重映射（默认引脚）"))
        else:
            result.append((item.value, f"{item.name}（{item.value}）"))
    return result


def tim_clock_source_options() -> list[tuple[str, str]]:
    """TIM 时钟源选项。

    标签用枚举自带的 ``label``（含中文说明），因为枚举名
    （``ETR_MODE1`` / ``TI1_ED`` / ``ITR0``）对不熟悉定时器的人不可读。
    """
    return [(item.value, item.label) for item in TimClockSource]


def tim_etr_prescaler_options() -> list[tuple[str, str]]:
    """ETR 外部触发预分频选项。"""
    return [(item.value, f"{item.name}（{item.label}）") for item in TimExtTriggerPrescaler]


def tim_etr_polarity_options() -> list[tuple[str, str]]:
    """ETR 外部触发极性选项。"""
    return [(item.value, item.label) for item in TimExtTriggerPolarity]


# --------------------------------------------------------------------------
# TIM 定时参数的自动反算
# --------------------------------------------------------------------------

# 频率与时间的输入解析用同一套单位表。为什么允许用户写单位：光看
# 「20000」分不清是 20kHz 还是 20Hz，而定时器配错一个数量级不会报错、
# 只会安静地跑错节拍 —— 让用户把单位写出来是最省的防错手段。
_TIME_UNITS: dict[str, float] = {
    "s": 1.0,
    "ms": 1e-3,
    "us": 1e-6,
    "µs": 1e-6,
    "ns": 1e-9,
}
_FREQ_UNITS: dict[str, float] = {
    "hz": 1.0,
    "khz": 1e3,
    "mhz": 1e6,
}

# 纯数字（无单位）默认按 Hz 解释 —— 与「期望更新频率」这个标签一致。
_NUMBER = r"([0-9]*\.?[0-9]+)"
_TIME_PATTERN = re.compile(rf"^\s*{_NUMBER}\s*(s|ms|us|µs|ns)\s*$", re.IGNORECASE)
_FREQ_PATTERN = re.compile(rf"^\s*{_NUMBER}\s*(hz|khz|mhz)?\s*$", re.IGNORECASE)


def parse_update_target(text: str) -> tuple[str, float]:
    """解析「期望更新频率/周期」输入，返回 ``("frequency", Hz)`` 或 ``("period", 秒)``。

    采纳单位后缀的输入形式，是因为定时参数错一个数量级时**没有任何报错**：
    PSC/ARR 写大 1000 倍照样能生成、能编译、能进芯片，只是节拍全错。
    强制用户写单位（或接受纯数字按 Hz）能把这种错挡在生成之前。
    """
    raw = text.strip()
    if not raw:
        raise ValueError("期望更新频率/周期不能为空")

    time_match = _TIME_PATTERN.match(raw)
    if time_match:
        value = float(time_match.group(1))
        seconds = value * _TIME_UNITS[time_match.group(2).lower()]
        if seconds <= 0:
            raise ValueError("期望周期必须大于 0")
        return ("period", seconds)

    freq_match = _FREQ_PATTERN.match(raw)
    if freq_match:
        value = float(freq_match.group(1))
        hz = value * _FREQ_UNITS[(freq_match.group(2) or "hz").lower()]
        if hz <= 0:
            raise ValueError("期望频率必须大于 0")
        return ("frequency", hz)

    raise ValueError(
        f"无法识别「{text}」：可写 1000、1kHz、20ms、0.5s 这样的形式"
    )


def timer_clock_hz(instance: str, hse_hz: int = 8_000_000) -> int:
    """该定时器实例的计数时钟（Hz）。

    **不是 PCLK**：APB 分频不为 1 时定时器时钟是 PCLK 的 2 倍。
    这里复用校验层的 ``compute_clock_tree``，保证「反算用的时钟」
    与「校验/生成用的时钟」是同一条时钟树，不会各算一套。

    GUI 目前不暴露 RCC 页（时钟树固定为默认的 72MHz 方案），所以这里
    用 :class:`RccConfig` 的默认值推导；若将来加了 RCC 页，把配置传进来
    即可，调用方接口不变。
    """
    from stm32gen.models.rcc import RccConfig
    from stm32gen.validate.baudrate import compute_clock_tree

    tree = compute_clock_tree(hse_hz, RccConfig())
    target = TimInstance(instance)
    return tree.tim1_hz if target.is_advanced else tree.tim234_hz


def solve_tim_timing(
    instance: str,
    text: str,
    *,
    hse_hz: int = 8_000_000,
    center_aligned: bool = False,
) -> TimTimingSolution:
    """把用户输入的期望频率/周期反算成 PSC/ARR。

    反算而不是让用户试凑的理由见 :func:`stm32gen.models.tim.solve_timing`：
    同一总分频有多种因数分解，占空比分辨率可能差几十倍。
    """
    kind, value = parse_update_target(text)
    clock = timer_clock_hz(instance, hse_hz)
    if kind == "period":
        return solve_period(clock, value, center_aligned=center_aligned)
    return solve_timing(clock, value, center_aligned=center_aligned)


def tim_timing_hint(
    instance: str,
    prescaler: str,
    period: str,
    counter_mode: str,
    *,
    hse_hz: int = 8_000_000,
) -> str:
    """按当前 PSC/ARR 回显实际更新频率与周期（供 GUI 状态栏用）。

    非法输入返回空串而不抛异常：这个函数挂在 ``editingFinished`` 上，
    用户打字过程中出现半截数字是常态，弹错误框会打断输入。
    """
    try:
        psc = parse_int(prescaler, name="预分频 PSC", minimum=0, maximum=TIM_REGISTER_MAX)
        arr = parse_int(period, name="重装载值 ARR", minimum=0, maximum=TIM_REGISTER_MAX)
    except ValueError:
        return ""

    clock = timer_clock_hz(instance, hse_hz)
    divider = (psc + 1) * (arr + 1)
    edge_factor = 2 if TimCounterMode(counter_mode).is_center_aligned else 1
    actual = clock / (divider * edge_factor)
    if actual <= 0:
        return ""
    return (
        f"时钟 {clock / 1e6:g}MHz ÷ {divider}"
        f"{' ÷2（中央对齐）' if edge_factor == 2 else ''} "
        f"= {_format_hz(actual)}（周期 {_format_seconds(1.0 / actual)}）"
    )


def _format_hz(hz: float) -> str:
    """频率写成人能读的单位（20kHz / 1kHz / 50Hz / 0.4Hz）。"""
    if hz >= 1e6:
        return f"{hz / 1e6:g}MHz"
    if hz >= 1e3:
        return f"{hz / 1e3:g}kHz"
    return f"{hz:g}Hz"


def _format_seconds(seconds: float) -> str:
    """秒数写成人能读的单位（2.5s / 20ms / 50us）。"""
    if seconds >= 1.0:
        return f"{seconds:g}s"
    if seconds >= 1e-3:
        return f"{seconds * 1e3:g}ms"
    if seconds >= 1e-6:
        return f"{seconds * 1e6:g}us"
    return f"{seconds * 1e9:g}ns"


# --------------------------------------------------------------------------
# 芯片相关的端口 / 引脚选项
#
# 这两组选项**取决于芯片封装**，不能写死：LQFP48 上只引出 PA0~PA15、
# PB0~PB15、PC13~PC15、PD0~PD1，GPIOE/F/G 根本不存在。
# 写死 A~G 的后果是用户能选中 GPIOE，然后只能等校验层报错 ——
# 把「封装上不存在」这种事留给用户去撞，是工具没尽责。
# --------------------------------------------------------------------------


def _chip_spec(chip_id: ChipId | str | None):
    """取芯片规格，接受 ``ChipId`` / 型号字符串 / ``None``（用默认芯片）。"""
    if chip_id is None:
        return get_chip(ChipId.F103C8T6)
    if isinstance(chip_id, ChipId):
        return get_chip(chip_id)
    return get_chip(ChipId(chip_id))


def port_options(chip_id: ChipId | str | None = None) -> list[tuple[str, str]]:
    """该芯片封装上**真实存在**的端口，标签带引脚数。

    引脚数直接写进标签，用户不必去翻数据手册就知道 PC 只有 3 个引脚。
    """
    spec = _chip_spec(chip_id)
    return [
        (f"GPIO{letter}", f"GPIO{letter}（{len(spec.ports[letter])} 引脚）")
        for letter in sorted(spec.ports)
    ]


def pin_note(chip_id: ChipId | str | None, letter: str, number: int) -> str:
    """该引脚的使用提示（晶振占用 / 调试口），没有则返回空串。

    只取提示里最前面那个短标记（``OSC_IN`` / ``SWDIO``），因为下拉框里
    需要的是能一眼扫过的词；完整原因由校验层的 ``pin.reserved`` /
    ``pin.debug_port`` 警告给出。

    调试口信息复用 ``validate.rules.DEBUG_PINS`` 而不是另抄一份：
    两份表一旦漂移，GUI 标了"可用"而校验层却报警，用户会失去信任。
    """
    spec = _chip_spec(chip_id)
    reason = spec.reserved_reason(letter, number)
    if reason:
        return reason.split("：")[0]
    role = DEBUG_PINS.get((letter, number))
    if role:
        return role.split("（")[0]
    return ""


def pin_options(
    chip_id: ChipId | str | None, port: str
) -> list[tuple[str, str]]:
    """该端口上可选的全部引脚号，``[(引脚号, 显示名)]``。

    值用**引脚号字符串**（``"9"``），与 ``GpioRow.pin`` 的既有约定一致，
    因此 ``parse_pin_number`` 与配置文件的往返都不受影响。

    被晶振/调试口占用的引脚**仍然列出**，只是标注出来 —— 它们电气上
    确实可用（比如板子没焊晶振时 PD0 完全可以当普通 IO），
    直接隐藏反而会让用户以为工具漏了引脚。取舍由用户做，风险由标注告知。
    """
    spec = _chip_spec(chip_id)
    try:
        letter = parse_port(port).letter
    except ValueError:
        return []
    numbers = sorted(spec.ports.get(letter, frozenset()))
    result: list[tuple[str, str]] = []
    for number in numbers:
        note = pin_note(chip_id, letter, number)
        result.append((str(number), f"{number}（{note}）" if note else str(number)))
    return result


# --------------------------------------------------------------------------
# 文本解析辅助
# --------------------------------------------------------------------------


def parse_port(text: str) -> GpioPort:
    """解析端口文本。

    接受四种写法，因为用户会从各处复制引脚名：

    - ``"GPIOA"`` 标准库符号名
    - ``"A"``     端口字母
    - ``"PA9"``   完整引脚名
    - ``"A9"``    字母+数字

    **注意不能简单地取第一个字母**：``"GPIOA"`` 的首字母是 ``G``，
    会解析成 ``GPIOG`` —— 这是个静默的错误结果，比报错更危险
    （初版就栽在这里，往返转换测试立刻抓到）。
    """
    cleaned = text.strip().upper()
    if not cleaned:
        raise ValueError("端口不能为空")

    # "GPIOA" / "gpioa" → 取 GPIO 之后的那个字母
    if cleaned.startswith("GPIO"):
        rest = cleaned[4:]
        if rest and rest[0].isalpha():
            return _letter_to_port(rest[0], text)
        raise ValueError(f"「{text}」缺少端口字母（应为 GPIOA~GPIOG）")

    # "PA9" → 跳过 P 前缀，取下一个字母。必须先判断这一步：
    # 若直接扫第一个字母会拿到 "P"，报「P 不是有效端口」。
    if cleaned.startswith("P") and len(cleaned) >= 2 and cleaned[1].isalpha():
        return _letter_to_port(cleaned[1], text)

    # "A9" / "A" → 取第一个字母
    for char in cleaned:
        if char.isalpha():
            return _letter_to_port(char, text)

    raise ValueError(f"无法从「{text}」解析出端口（可用 A~G）")


def _letter_to_port(letter: str, original: str) -> GpioPort:
    """把单个字母转成端口枚举，并给出友好的错误信息。"""
    try:
        return GpioPort(letter)
    except ValueError as exc:
        raise ValueError(
            f"「{letter}」不是有效端口（来自「{original}」，可用 A~G）"
        ) from exc


def parse_pin_number(text: str) -> int:
    """解析引脚号。

    接受 ``"9"``、``"PA9"``、``"pin9"``、``"GPIOA, 9"`` 等写法。

    **先剥离端口部分再取数字**：``"GPIOA"`` 里的字母不含数字会报错，
    但 ``"PIN9"`` 要去掉 ``PIN`` 前缀，否则会误判。这里统一用
    「移除已知前缀 → 取全部数字字符」的策略。
    """
    cleaned = text.strip().upper()
    for prefix in ("GPIO", "PIN", "P"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    digits = "".join(char for char in cleaned if char.isdigit())
    if not digits:
        raise ValueError(f"无法从「{text}」解析出引脚号（应为 0~15）")
    number = int(digits)
    if not 0 <= number <= 15:
        raise ValueError(f"引脚号 {number} 超出 0~15")
    return number


def parse_pin_name(text: str) -> tuple[GpioPort, int]:
    """解析 ``"PA9"`` 形式的完整引脚名。"""
    return parse_port(text), parse_pin_number(text)


def parse_int(text: str, *, name: str, minimum: int, maximum: int) -> int:
    """解析整数并检查范围。"""
    cleaned = text.strip().replace(",", "").replace("_", "")
    if not cleaned:
        raise ValueError(f"{name}不能为空")
    try:
        value = int(cleaned, 0)  # 允许 0x 前缀
    except ValueError as exc:
        raise ValueError(f"{name}「{text}」不是有效整数") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} {value} 超出范围 {minimum}~{maximum}")
    return value


# --------------------------------------------------------------------------
# GPIO 行 ↔ 模型
# --------------------------------------------------------------------------


@dataclass
class GpioRow:
    """GPIO 表格的一行（全部是原始文本，与控件内容一一对应）。"""

    port: str = "GPIOA"
    pin: str = "0"
    mode: str = GpioMode.Out_PP.value
    speed: str = GpioSpeed.SPEED_50MHz.value
    init_level: str = GpioInitLevel.LOW.value
    label: str = ""

    @classmethod
    def from_config(cls, pin: GpioPinConfig) -> GpioRow:
        """由模型构造一行，用于「加载配置 → 填表」。"""
        return cls(
            port=pin.port.value,
            pin=str(pin.pin),
            mode=pin.mode.value,
            speed=pin.speed.value,
            init_level=(
                pin.init_level.value if pin.init_level is not None
                else GpioInitLevel.LOW.value
            ),
            label=pin.label,
        )

    @property
    def mode_is_input(self) -> bool:
        """当前模式是否输入（输入模式不需要初始电平与速度）。"""
        try:
            return GpioMode(self.mode).is_input
        except ValueError:
            return False

    def to_config(self) -> GpioPinConfig:
        """转换成模型；失败时抛 ``ValidationError`` 或 ``ValueError``。"""
        return GpioPinConfig(
            port=parse_port(self.port),
            pin=parse_pin_number(self.pin),
            mode=GpioMode(self.mode),
            speed=GpioSpeed(self.speed),
            # 输入模式必须给 None，否则模型会报「不应指定 init_level」
            init_level=None if self.mode_is_input else GpioInitLevel(self.init_level),
            label=self.label,
        )


# --------------------------------------------------------------------------
# USART ↔ 模型
# --------------------------------------------------------------------------


@dataclass
class UsartForm:
    """USART 表单的全部字段（原始文本 / 选项值）。"""

    instance: str = UsartInstance.USART1.value
    baudrate: str = "115200"
    word_length: str = UsartWordLength.B8.value
    stop_bits: str = UsartStopBits.B1.value
    parity: str = UsartParity.NONE.value
    direction: str = UsartDirection.RX_TX.value
    flow_control: str = UsartFlowControl.NONE.value
    interrupts: list[str] = field(default_factory=list)
    remap: str = UsartRemap.NONE.value
    tx_pin: str = ""
    rx_pin: str = ""
    label: str = ""

    @classmethod
    def from_config(cls, usart: UsartConfig) -> UsartForm:
        return cls(
            instance=usart.instance.value,
            baudrate=str(usart.baudrate),
            word_length=usart.word_length.value,
            stop_bits=usart.stop_bits.value,
            parity=usart.parity.value,
            direction=usart.direction.value,
            flow_control=usart.flow_control.value,
            interrupts=[item.value for item in usart.interrupts],
            remap=usart.remap.value,
            tx_pin="" if usart.tx_pin is None else str(usart.tx_pin),
            rx_pin="" if usart.rx_pin is None else str(usart.rx_pin),
            label=usart.label,
        )

    def to_config(self) -> UsartConfig:
        """转换成模型。

        两个可选引脚留空表示「用默认引脚」，所以空串要转成 ``None`` 而不是 0
        —— 0 是合法引脚号，混起来会把 PA9 变成 PA0。
        """
        from stm32gen.models.usart import BAUDRATE_MAX_HINT

        return UsartConfig(
            instance=UsartInstance(self.instance),
            baudrate=parse_int(
                self.baudrate, name="波特率", minimum=1200, maximum=BAUDRATE_MAX_HINT
            ),
            word_length=UsartWordLength(self.word_length),
            stop_bits=UsartStopBits(self.stop_bits),
            parity=UsartParity(self.parity),
            direction=UsartDirection(self.direction),
            flow_control=UsartFlowControl(self.flow_control),
            interrupts=[UsartInterrupt(item) for item in self.interrupts],
            remap=UsartRemap(self.remap),
            tx_pin=self._parse_optional_pin(self.tx_pin, "TX"),
            rx_pin=self._parse_optional_pin(self.rx_pin, "RX"),
            label=self.label,
        )

    @staticmethod
    def _parse_optional_pin(text: str, role: str) -> int | None:
        """解析可选的引脚号；空串 → None（表示用默认引脚）。"""
        if not text.strip():
            return None
        return parse_int(text, name=f"{role} 引脚号", minimum=0, maximum=15)


# --------------------------------------------------------------------------
# NVIC 通道 ↔ 模型
# --------------------------------------------------------------------------


@dataclass
class NvicRow:
    """NVIC 表格的一行。"""

    irq_channel: str = "USART1_IRQn"
    preemption: str = "0"
    sub: str = "0"
    enabled: bool = True
    for_peripheral: str = ""

    @classmethod
    def from_config(cls, channel: NvicChannelConfig) -> NvicRow:
        return cls(
            irq_channel=channel.irq_channel,
            preemption=str(channel.preemption_priority),
            sub=str(channel.sub_priority),
            enabled=channel.enabled,
            for_peripheral=channel.for_peripheral,
        )

    def to_config(self) -> NvicChannelConfig:
        return NvicChannelConfig(
            irq_channel=self.irq_channel.strip(),
            preemption_priority=parse_int(
                self.preemption, name="抢占优先级", minimum=0, maximum=15
            ),
            sub_priority=parse_int(self.sub, name="子优先级", minimum=0, maximum=15),
            enabled=self.enabled,
            for_peripheral=self.for_peripheral,
        )


# --------------------------------------------------------------------------
# EXTI / TIM / AFIO ↔ 模型
# --------------------------------------------------------------------------


@dataclass
class ExtiRow:
    """EXTI 表格的一行。"""

    line: str = "0"
    port: str = "GPIOA"
    mode: str = ExtiMode.INTERRUPT.value
    trigger: str = ExtiTrigger.FALLING.value
    label: str = ""

    @classmethod
    def from_config(cls, item: ExtiLineConfig) -> ExtiRow:
        return cls(
            line=str(item.line),
            port=item.port.value,
            mode=item.mode.value,
            trigger=item.trigger.value,
            label=item.label,
        )

    def to_config(self) -> ExtiLineConfig:
        return ExtiLineConfig(
            line=parse_int(
                self.line,
                name="EXTI 线号",
                minimum=EXTI_EXTERNAL_LINE_MIN,
                maximum=EXTI_EXTERNAL_LINE_MAX,
            ),
            port=parse_port(self.port),
            mode=ExtiMode(self.mode),
            trigger=ExtiTrigger(self.trigger),
            label=self.label,
        )


@dataclass
class TimChannelRow:
    """TIM 通道表格的一行。"""

    channel: str = TimChannel.CH1.value
    mode: str = TimOcMode.PWM1.value
    pulse: str = "0"
    polarity: str = TimOcPolarity.HIGH.value
    preload: str = TimOcPreload.ENABLE.value
    output_state: str = TimOutputState.ENABLE.value
    port: str = ""
    pin: str = ""
    label: str = ""

    @classmethod
    def from_config(cls, item: TimChannelConfig) -> TimChannelRow:
        return cls(
            channel=item.channel.value,
            mode=item.mode.value,
            pulse=str(item.pulse),
            polarity=item.polarity.value,
            preload=item.preload.value,
            output_state=item.output_state.value,
            port=item.port.value if item.port is not None else "",
            pin="" if item.pin is None else str(item.pin),
            label=item.label,
        )

    def to_config(self) -> TimChannelConfig:
        """转换成模型。

        端口与引脚必须**同时**给或同时留空（模型层会拦），
        所以这里只在两者都填了时才解析 —— 只填一个时交给模型报错，
        错误信息更准确（模型会说"必须同时指定"）。
        """
        port_text = self.port.strip()
        pin_text = self.pin.strip()
        port = parse_port(port_text) if port_text else None
        pin = (
            parse_int(pin_text, name="引脚号", minimum=0, maximum=15)
            if pin_text
            else None
        )
        return TimChannelConfig(
            channel=TimChannel(self.channel),
            mode=TimOcMode(self.mode),
            pulse=parse_int(
                self.pulse, name="比较值 CCR", minimum=0, maximum=TIM_REGISTER_MAX
            ),
            polarity=TimOcPolarity(self.polarity),
            preload=TimOcPreload(self.preload),
            output_state=TimOutputState(self.output_state),
            port=port,
            pin=pin,
            label=self.label,
        )


@dataclass
class TimForm:
    """TIM 表单（一个实例 + 它的通道列表）。"""

    instance: str = TimInstance.TIM2.value
    prescaler: str = "71"
    period: str = "999"
    counter_mode: str = TimCounterMode.UP.value
    clock_division: str = TimClockDivision.DIV1.value
    repetition_counter: str = "0"
    interrupts: list[str] = field(default_factory=list)
    remap: str = TimRemap.NONE.value
    label: str = ""
    channels: list[TimChannelRow] = field(default_factory=list)
    # 时钟源。INTERNAL 时下面四项都不生效，模型层会拒绝「内部时钟却填了
    # ETR 参数」的组合 —— 静默忽略比报错更糟，用户会以为配上了。
    clock_source: str = TimClockSource.INTERNAL.value
    etr_prescaler: str = TimExtTriggerPrescaler.OFF.value
    etr_polarity: str = TimExtTriggerPolarity.NON_INVERTED.value
    etr_filter: str = "0"
    external_clock_hz: str = ""

    @property
    def uses_external_clock(self) -> bool:
        """表单当前是否选了外部时钟。"""
        return self.clock_source != TimClockSource.INTERNAL.value

    @classmethod
    def from_config(cls, item: TimConfig) -> TimForm:
        return cls(
            instance=item.instance.value,
            prescaler=str(item.prescaler),
            period=str(item.period),
            counter_mode=item.counter_mode.value,
            clock_division=item.clock_division.value,
            repetition_counter=str(item.repetition_counter),
            interrupts=[i.value for i in item.interrupts],
            remap=item.remap.value,
            label=item.label,
            channels=[TimChannelRow.from_config(c) for c in item.channels],
            clock_source=item.clock_source.value,
            etr_prescaler=item.etr_prescaler.value,
            etr_polarity=item.etr_polarity.value,
            etr_filter=str(item.etr_filter),
            external_clock_hz=(
                f"{item.external_clock_hz:g}" if item.external_clock_hz else ""
            ),
        )

    def to_config(self) -> TimConfig:
        # 外部时钟频率是可选的：留空表示"不知道"，不影响生成，
        # 只是工具算不出实际更新频率。填了就必须是正数。
        clock_text = self.external_clock_hz.strip()
        external_hz = (
            parse_int(
                clock_text,
                name="外部时钟频率",
                minimum=1,
                maximum=100_000_000,
            )
            if clock_text
            else None
        )
        return TimConfig(
            instance=TimInstance(self.instance),
            prescaler=parse_int(
                self.prescaler, name="预分频 PSC", minimum=0, maximum=TIM_REGISTER_MAX
            ),
            period=parse_int(
                self.period, name="重装载值 ARR", minimum=0, maximum=TIM_REGISTER_MAX
            ),
            counter_mode=TimCounterMode(self.counter_mode),
            clock_division=TimClockDivision(self.clock_division),
            repetition_counter=parse_int(
                self.repetition_counter, name="重复计数 RCR", minimum=0, maximum=0xFF
            ),
            channels=[c.to_config() for c in self.channels],
            interrupts=[TimInterrupt(i) for i in self.interrupts],
            remap=TimRemap(self.remap),
            label=self.label,
            clock_source=TimClockSource(self.clock_source),
            etr_prescaler=TimExtTriggerPrescaler(self.etr_prescaler),
            etr_polarity=TimExtTriggerPolarity(self.etr_polarity),
            etr_filter=parse_int(
                self.etr_filter,
                name="外部信号滤波",
                minimum=0,
                maximum=TIM_EXT_FILTER_MAX,
            ),
            external_clock_hz=external_hz,
        )


@dataclass
class AfioForm:
    """AFIO 表单（只有 SWJ 释放一项可配）。"""

    swj_remap: str = AfioSwjRemap.NONE.value

    @classmethod
    def from_config(cls, item: AfioConfig) -> AfioForm:
        return cls(swj_remap=item.swj_remap.value)

    def to_config(self) -> AfioConfig:
        """转换成模型。

        ``enabled`` 不在这里传 —— ``AfioConfig.__init__`` 会按 ``swj_remap``
        自动推导（选了释放就启用）。让用户额外回答"要不要启用"是多余的，
        而且两个答案矛盾时会生成"看起来已释放但代码里没有"的配置。
        """
        return AfioConfig(swj_remap=AfioSwjRemap(self.swj_remap))


# --------------------------------------------------------------------------
# 整体配置 ↔ 表单数据
# --------------------------------------------------------------------------


@dataclass
class ConfigForm:
    """整个配置的表单表示。

    这是 GUI 与模型之间的唯一数据通道：GUI 只填这里，再由 ``to_config()``
    生成模型。这样 GUI 不需要知道模型字段名，模型改字段也不会波及控件代码。
    """

    chip: str = ChipId.F103C8T6.value
    hse_hz: str = "8000000"
    project_name: str = "stm32_init"
    priority_group: str = NvicPriorityGroup.GROUP2.value
    gpio_rows: list[GpioRow] = field(default_factory=list)
    usart_forms: list[UsartForm] = field(default_factory=list)
    nvic_rows: list[NvicRow] = field(default_factory=list)
    afio: AfioForm = field(default_factory=AfioForm)
    exti_rows: list[ExtiRow] = field(default_factory=list)
    tim_forms: list[TimForm] = field(default_factory=list)

    @classmethod
    def from_config(cls, config: Stm32Config) -> ConfigForm:
        """由模型构造表单数据，用于「打开配置文件」。"""
        return cls(
            chip=config.chip.value,
            hse_hz=str(config.hse_hz),
            project_name=config.project_name,
            priority_group=config.nvic.priority_group.value,
            gpio_rows=[GpioRow.from_config(pin) for pin in config.gpio],
            usart_forms=[UsartForm.from_config(item) for item in config.usart],
            nvic_rows=[
                NvicRow.from_config(channel) for channel in config.nvic.channels
            ],
            afio=AfioForm.from_config(config.afio),
            exti_rows=[ExtiRow.from_config(item) for item in config.exti],
            tim_forms=[TimForm.from_config(item) for item in config.tim],
        )

    def to_config(self) -> ConfigResult:
        """转换成模型，**收集全部错误**而不是遇错即停。

        GUI 需要一次把所有字段的错误都标出来，逐个报错会让用户
        改一处、点一次、再改一处。
        """
        errors: list[FieldError] = []

        hse = 8_000_000
        try:
            hse = parse_int(
                self.hse_hz, name="晶振频率", minimum=1_000_000, maximum=25_000_000
            )
        except ValueError as exc:
            errors.append(FieldError("hse_hz", str(exc)))

        gpio: list[GpioPinConfig] = []
        for index, row in enumerate(self.gpio_rows):
            try:
                gpio.append(row.to_config())
            except (ValueError, ValidationError) as exc:
                errors.append(FieldError(f"gpio[{index}]", _clean_error(exc)))

        usart: list[UsartConfig] = []
        for index, form in enumerate(self.usart_forms):
            try:
                usart.append(form.to_config())
            except (ValueError, ValidationError) as exc:
                errors.append(FieldError(f"usart[{index}]", _clean_error(exc)))

        channels: list[NvicChannelConfig] = []
        for index, row in enumerate(self.nvic_rows):
            try:
                channels.append(row.to_config())
            except (ValueError, ValidationError) as exc:
                errors.append(FieldError(f"nvic[{index}]", _clean_error(exc)))

        exti: list[ExtiLineConfig] = []
        for index, row in enumerate(self.exti_rows):
            try:
                exti.append(row.to_config())
            except (ValueError, ValidationError) as exc:
                errors.append(FieldError(f"exti[{index}]", _clean_error(exc)))

        tim: list[TimConfig] = []
        for index, form in enumerate(self.tim_forms):
            try:
                tim.append(form.to_config())
            except (ValueError, ValidationError) as exc:
                errors.append(FieldError(f"tim[{index}]", _clean_error(exc)))

        try:
            afio = self.afio.to_config()
        except (ValueError, ValidationError) as exc:
            errors.append(FieldError("afio", _clean_error(exc)))
            afio = AfioConfig()

        if errors:
            return ConfigResult(value=None, errors=errors)

        try:
            config = Stm32Config(
                chip=ChipId(self.chip),
                hse_hz=hse,
                project_name=self.project_name,
                gpio=gpio,
                usart=usart,
                nvic=NvicConfig(
                    priority_group=NvicPriorityGroup(self.priority_group),
                    channels=channels,
                ),
                afio=afio,
                exti=exti,
                tim=tim,
            )
        except ValidationError as exc:
            # 跨字段约束（如「输出模式必须给初始电平」在 row 里已处理，
            # 但芯片级别的检查在此触发）
            for item in exc.errors():
                location = ".".join(str(part) for part in item["loc"]) or "<根>"
                errors.append(FieldError(location, item["msg"]))
            return ConfigResult(value=None, errors=errors)

        return ConfigResult(value=config, errors=[])


@dataclass
class ConfigResult:
    """整体配置的转换结果。"""

    value: Stm32Config | None = None
    errors: list[FieldError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.value is not None and not self.errors

    def format_errors(self, limit: int = 8) -> str:
        """把错误列表汇总成多行文本（供状态栏或弹窗）。"""
        lines = [item.format() for item in self.errors[:limit]]
        if len(self.errors) > limit:
            lines.append(f"… 另有 {len(self.errors) - limit} 处问题")
        return "\n".join(lines)


def _clean_error(exc: Exception) -> str:
    """把 pydantic 的冗长错误信息压成一句话。

    ``ValidationError`` 的 ``str()`` 会带上 URL 与文档链接，
    放进 GUI 单元格里太长，这里只取 ``msg``。
    """
    if isinstance(exc, ValidationError):
        parts = []
        for item in exc.errors():
            location = ".".join(str(part) for part in item["loc"])
            parts.append(f"{location}: {item['msg']}" if location else item["msg"])
        return "；".join(parts)
    return str(exc)
