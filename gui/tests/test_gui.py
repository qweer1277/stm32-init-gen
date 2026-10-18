"""GUI 层的测试。

分两部分：

1. **桥接层**（``stm32gen_gui.bridge``）—— 不依赖 Qt，可完整覆盖。
   这是 GUI 里最容易出错的部分（表格文本 ↔ 模型的双向转换），
   把它隔离出来就是为了能在无显示器环境里测。
2. **Qt 组件** —— 用 ``QT_QPA_PLATFORM=offscreen`` 跑，验证控件能构造、
   数据能正确填充、动作不崩。不做像素级断言（那属于人工验收）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# 必须在导入任何 PySide6 模块之前设置，否则会尝试连接真实显示
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from stm32gen.models import (  # noqa: E402
    ChipId,
    GpioMode,
    GpioPinConfig,
    GpioPort,
    Stm32Config,
    UsartInterrupt,
    make_demo_config,
)

from stm32gen_gui import bridge  # noqa: E402

# --------------------------------------------------------------------------
# 桥接层：文本解析
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("GPIOA", GpioPort.A),
        ("gpioa", GpioPort.A),
        ("A", GpioPort.A),
        ("a", GpioPort.A),
        ("PA9", GpioPort.A),
        ("A9", GpioPort.A),
        ("C", GpioPort.C),
        ("GPIOC", GpioPort.C),
        ("c", GpioPort.C),
        (" PC13 ", GpioPort.C),  # 前后空白
    ],
)
def test_parse_port_variants(text: str, expected: GpioPort) -> None:
    """端口解析必须容错。

    **关键回归点**：``"GPIOA"`` 的首字母是 ``G``，若简单地取第一个字母
    会解析成 ``GPIOG`` —— 一个静默的错误结果，比报错更危险。
    初版就栽在这里，往返转换测试立刻抓到。
    """
    assert parse_port(text) is expected


def parse_port(text: str) -> GpioPort:
    return bridge.parse_port(text)


def test_parse_port_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        bridge.parse_port("")
    with pytest.raises(ValueError):
        bridge.parse_port("Z")
    with pytest.raises(ValueError):
        bridge.parse_port("123")


def test_parse_port_accepts_all_letters_even_unavailable_ones() -> None:
    """``parse_port`` 只做**文本 → 枚举**的转换，不管芯片是否引出该端口。

    ``GPIOD``/``GPIOE`` 都是合法的端口字母，解析层不该拒绝它们 ——
    「LQFP48 没引出 GPIOE」属于芯片级约束，由模型层与校验层负责报错。
    分层不清会让错误信息出现在错误的地方（用户会在引脚输入框上
    看到「芯片不支持」，而问题其实在芯片选择上）。
    """
    assert bridge.parse_port("GPIOD") is GpioPort.D
    assert bridge.parse_port("GPIOE") is GpioPort.E


@pytest.mark.parametrize(
    ("text", "expected"),
    [("9", 9), ("0", 0), ("15", 15), ("PA9", 9), ("pin9", 9), ("GPIOA 9", 9)],
)
def test_parse_pin_number_variants(text: str, expected: int) -> None:
    assert bridge.parse_pin_number(text) == expected


def test_parse_pin_number_rejects_out_of_range() -> None:
    with pytest.raises(ValueError, match="超出"):
        bridge.parse_pin_number("16")
    with pytest.raises(ValueError):
        bridge.parse_pin_number("abc")


def test_parse_int_accepts_hex_and_separators() -> None:
    """整数解析要容忍 0x 前缀与千位分隔符（用户会从各处复制）。"""
    assert bridge.parse_int("115200", name="波特率", minimum=0, maximum=1e9) == 115200
    assert bridge.parse_int("0x1C200", name="波特率", minimum=0, maximum=1e9) == 115200
    assert bridge.parse_int("115,200", name="波特率", minimum=0, maximum=1e9) == 115200


def test_parse_int_range_check() -> None:
    with pytest.raises(ValueError, match="超出范围"):
        bridge.parse_int("999", name="引脚", minimum=0, maximum=15)
    with pytest.raises(ValueError, match="不是有效整数"):
        bridge.parse_int("abc", name="值", minimum=0, maximum=15)
    with pytest.raises(ValueError, match="不能为空"):
        bridge.parse_int("", name="值", minimum=0, maximum=15)


# --------------------------------------------------------------------------
# 桥接层：选项列表（供下拉框使用）
# --------------------------------------------------------------------------


def test_option_lists_are_non_empty_and_unique() -> None:
    """每个选项列表都要非空、值唯一，否则下拉框会有重复项。"""
    for func in (
        bridge.mode_options,
        bridge.speed_options,
        bridge.init_level_options,
        bridge.usart_instance_options,
        bridge.word_length_options,
        bridge.stop_bits_options,
        bridge.parity_options,
        bridge.direction_options,
        bridge.flow_control_options,
        bridge.interrupt_options,
        bridge.remap_options,
        bridge.priority_group_options,
        bridge.chip_options,
        bridge.port_options,
    ):
        options = func()
        assert options, f"{func.__name__} 返回空列表"
        values = [value for value, _ in options]
        assert len(values) == len(set(values)), f"{func.__name__} 有重复值"


def test_port_options_label_includes_pin_count() -> None:
    """端口选项要带引脚数，用户不用翻数据手册就知道 PC 只有 3 个。"""
    options = dict(bridge.port_options())
    assert options["GPIOA"] == "GPIOA（16 引脚）"
    assert options["GPIOC"] == "GPIOC（3 引脚）"
    assert options["GPIOD"] == "GPIOD（2 引脚）"


def test_port_options_only_lists_existing_ports() -> None:
    """LQFP48 没有 GPIOE/F/G，选项里就不该有。"""
    values = [value for value, _ in bridge.port_options()]
    assert values == ["GPIOA", "GPIOB", "GPIOC", "GPIOD"]
    assert "GPIOE" not in values


def test_pin_options_cover_the_whole_port() -> None:
    assert len(bridge.pin_options(None, "GPIOA")) == 16
    assert len(bridge.pin_options(None, "GPIOC")) == 3


def test_pin_options_values_are_bare_numbers() -> None:
    """值是裸引脚号字符串，与 ``GpioRow.pin`` 的既有约定一致。

    若改成 ``"PA9"`` 这种完整名，``parse_pin_number`` 虽然仍能解析，
    但配置文件往返与 ``GpioRow`` 的语义就变了 —— 没必要动。
    """
    values = [value for value, _ in bridge.pin_options(None, "GPIOA")]
    assert values[:3] == ["0", "1", "2"]
    assert all(value.isdigit() for value in values)


def test_pin_options_marks_reserved_and_debug_pins() -> None:
    """晶振与调试口引脚要标注，但仍然可选。"""
    options = dict(bridge.pin_options(None, "GPIOD"))
    assert "OSC_IN" in options["0"]
    assert "OSC_OUT" in options["1"]

    pa = dict(bridge.pin_options(None, "GPIOA"))
    assert "SWDIO" in pa["13"]
    assert "SWCLK" in pa["14"]


def test_pin_options_normal_pins_have_plain_labels() -> None:
    """普通引脚的标签就是引脚号本身，不加多余括号。"""
    options = dict(bridge.pin_options(None, "GPIOA"))
    assert options["5"] == "5"


def test_pin_options_unknown_port_is_empty() -> None:
    """不存在的端口返回空列表而不是抛异常 —— GUI 重建下拉时会走到这里。"""
    assert bridge.pin_options(None, "GPIOE") == []
    assert bridge.pin_options(None, "不是端口") == []


def test_pin_note_agrees_with_validator() -> None:
    """GUI 的引脚标注必须与校验层的判定一致。

    两处各有一份数据的话，GUI 标"可用"而校验层却报警，用户会失去信任。
    ``bridge.pin_note`` 直接复用 ``validate.rules.DEBUG_PINS`` 就是为了这个。
    """
    from stm32gen.models import Stm32Config
    from stm32gen.validate import Severity, validate

    # PA13 是调试口：GUI 标注它，校验层也必须报 pin.debug_port
    assert bridge.pin_note(None, "A", 13) == "SWDIO"
    issues = validate(Stm32Config(gpio=[GpioPinConfig(port="A", pin=13, mode="Out_PP",
                                                     speed="SPEED_2MHz",
                                                     init_level="Bit_SET")]))
    assert any(
        item.code == "pin.debug_port" and item.severity is Severity.WARNING
        for item in issues
    )

    # 普通引脚两边都无话可说
    assert bridge.pin_note(None, "A", 5) == ""


def test_mode_options_cover_all_modes() -> None:
    assert len(bridge.mode_options()) == len(GpioMode)


def test_priority_group_options_include_bit_widths() -> None:
    """分组选项要显示位宽，用户不用记 0~4 各是什么。"""
    options = dict(bridge.priority_group_options())
    assert "抢占 2 位" in options["NVIC_PriorityGroup_2"]
    assert "0~3" in options["NVIC_PriorityGroup_2"]


def test_interrupt_options_have_chinese_labels() -> None:
    labels = dict(bridge.interrupt_options())
    assert "收到数据" in labels["USART_IT_RXNE"]
    assert "总线空闲" in labels["USART_IT_IDLE"]


# --------------------------------------------------------------------------
# 桥接层：GPIO 行
# --------------------------------------------------------------------------


def test_gpio_row_roundtrip() -> None:
    """GPIO 行 → 模型 → 行，必须完全等价。"""
    original = bridge.GpioRow(
        port="GPIOC",
        pin="13",
        mode="GPIO_Mode_Out_PP",
        speed="GPIO_Speed_50MHz",
        init_level="Bit_SET",
        label="LED1",
    )
    config = original.to_config()
    restored = bridge.GpioRow.from_config(config)
    assert restored == original


def test_gpio_row_input_mode_without_level() -> None:
    """输入模式转模型时 init_level 必须是 None。

    否则模型会报「输入模式不应指定 init_level」—— 表单里那个下拉框
    对输入模式没有意义，但控件里仍有值，转换层必须负责丢掉它。
    """
    row = bridge.GpioRow(port="A", pin="1", mode="GPIO_Mode_IPU")
    pin = row.to_config()
    assert pin.init_level is None
    assert pin.mode is GpioMode.IPU


def test_gpio_row_mode_is_input() -> None:
    assert bridge.GpioRow(mode="GPIO_Mode_IPU").mode_is_input
    assert bridge.GpioRow(mode="GPIO_Mode_IN_FLOATING").mode_is_input
    assert not bridge.GpioRow(mode="GPIO_Mode_Out_PP").mode_is_input
    # 非法值时保守返回 False（控件还没填完）
    assert not bridge.GpioRow(mode="").mode_is_input


def test_gpio_row_accepts_pa9_style_pin_text() -> None:
    """引脚列里填 "PA9" 也能解析 —— 用户常整段复制引脚名。"""
    row = bridge.GpioRow(port="A", pin="PA9", mode="GPIO_Mode_Out_PP")
    pin = row.to_config()
    assert pin.pin == 9


# --------------------------------------------------------------------------
# 桥接层：USART 表单
# --------------------------------------------------------------------------


def test_usart_form_roundtrip() -> None:
    from stm32gen.models import UsartConfig

    original = UsartConfig(
        instance="USART1",
        baudrate=9600,
        interrupts=["RXNE", "IDLE"],
        label="调试口",
    )
    form = bridge.UsartForm.from_config(original)
    restored = form.to_config()
    # 注意：to_dict() 只有 Stm32Config 有，子模型用 model_dump(mode="json")
    assert restored.model_dump(mode="json") == original.model_dump(mode="json")


def test_usart_form_empty_pins_mean_default() -> None:
    """TX/RX 留空表示「用默认引脚」，必须转成 None 而不是 0。

    0 是合法引脚号：混起来会把「不指定」变成 PA0。
    """
    form = bridge.UsartForm(instance="USART1", tx_pin="", rx_pin="")
    config = form.to_config()
    assert config.tx_pin is None
    assert config.rx_pin is None
    # 生效引脚应为默认值
    assert config.effective_tx_pin == ("A", 9)


def test_usart_form_explicit_zero_pin() -> None:
    """显式填 0 应该是 PA0，不是「未指定」。"""
    form = bridge.UsartForm(instance="USART2", tx_pin="0", rx_pin="0")
    config = form.to_config()
    assert config.tx_pin == 0
    assert config.rx_pin == 0
    assert config.effective_tx_pin == ("A", 0)


def test_usart_form_interrupts() -> None:
    form = bridge.UsartForm(instance="USART1", interrupts=["USART_IT_RXNE"])
    config = form.to_config()
    assert config.interrupts == [UsartInterrupt.RXNE]


def test_usart_form_invalid_baudrate_raises() -> None:
    form = bridge.UsartForm(instance="USART1", baudrate="abc")
    with pytest.raises(ValueError):
        form.to_config()


# --------------------------------------------------------------------------
# 桥接层：NVIC 行
# --------------------------------------------------------------------------


def test_nvic_row_roundtrip() -> None:
    from stm32gen.models import NvicChannelConfig

    original = NvicChannelConfig(
        irq_channel="USART1_IRQn",
        preemption_priority=2,
        sub_priority=1,
        for_peripheral="USART1",
    )
    row = bridge.NvicRow.from_config(original)
    restored = row.to_config()
    assert restored.model_dump(mode="json") == original.model_dump(mode="json")


def test_nvic_row_enabled_flag() -> None:
    assert bridge.NvicRow(enabled=True).to_config().enabled
    assert not bridge.NvicRow(enabled=False).to_config().enabled


# --------------------------------------------------------------------------
# 桥接层：整体配置
# --------------------------------------------------------------------------


def test_config_form_roundtrip() -> None:
    """整份配置 → 表单 → 配置，必须逐字段等价。"""
    original = make_demo_config()
    form = bridge.ConfigForm.from_config(original)
    result = form.to_config()

    assert result.ok, result.format_errors()
    assert result.value is not None
    assert result.value.to_dict() == original.to_dict()


def test_empty_config_form_roundtrip() -> None:
    original = Stm32Config()
    form = bridge.ConfigForm.from_config(original)
    result = form.to_config()
    assert result.ok
    assert result.value.to_dict() == original.to_dict()


def test_config_form_collects_all_errors() -> None:
    """转换失败时要**收集全部错误**，而不是遇错即停。

    GUI 需要一次把所有字段标红；逐个报错会让用户改一处、点一次、再改一处。
    """
    form = bridge.ConfigForm(
        hse_hz="abc",
        gpio_rows=[
            bridge.GpioRow(port="Z", pin="99"),          # 端口非法
            bridge.GpioRow(port="A", pin="abc"),         # 引脚非法
        ],
        usart_forms=[
            bridge.UsartForm(instance="USART1", baudrate="xyz"),  # 波特率非法
        ],
        nvic_rows=[
            bridge.NvicRow(irq_channel="USART1_IRQn", preemption="bad"),
        ],
    )
    result = form.to_config()
    assert not result.ok
    # 四处问题都要报到
    assert len(result.errors) >= 4
    fields = {item.field for item in result.errors}
    assert "hse_hz" in fields
    assert any(field.startswith("gpio[") for field in fields)
    assert any(field.startswith("usart[") for field in fields)
    assert any(field.startswith("nvic[") for field in fields)


def test_config_form_reports_cross_field_errors() -> None:
    """跨字段约束（芯片级检查）的错误也要被捕获。

    PC12 在 LQFP48 上未引出，属于 ``Stm32Config`` 的跨字段校验。
    这类错误的 ``loc`` 是**根节点**（因为它不绑定单个字段），
    所以 field 是 ``<根>`` 而不是 ``gpio[...]`` —— 断言不该写窄。
    """
    form = bridge.ConfigForm(
        chip=ChipId.F103C8T6.value,
        gpio_rows=[
            bridge.GpioRow(
                port="GPIOC",
                pin="12",
                mode="GPIO_Mode_Out_PP",
                speed="GPIO_Speed_2MHz",
                init_level="Bit_RESET",
            )
        ],
    )
    result = form.to_config()
    assert not result.ok
    assert result.errors
    # 错误信息里要点出具体引脚，否则用户不知道改哪个
    assert any("PC12" in item.message for item in result.errors)
    # 不该把 pydantic 的文档链接带进 GUI
    assert all("https://" not in item.message for item in result.errors)


def test_config_form_error_message_is_compact() -> None:
    """错误信息不能带上 pydantic 的 URL 与文档链接（放进单元格太长）。"""
    form = bridge.ConfigForm(
        gpio_rows=[bridge.GpioRow(port="A", pin="0", mode="GPIO_Mode_IPU")],
        chip=ChipId.F103C6T6.value,
    )
    result = form.to_config()
    if not result.ok:
        for item in result.errors:
            assert "https://" not in item.message
            assert "For further information" not in item.message


def test_config_form_format_errors_truncates() -> None:
    """错误过多时汇总要截断，避免撑爆提示框。"""
    form = bridge.ConfigForm(
        gpio_rows=[bridge.GpioRow(port="Z", pin="99") for _ in range(20)]
    )
    result = form.to_config()
    text = result.format_errors(limit=5)
    assert "另有" in text


def test_config_result_ok_flag() -> None:
    ok_result = bridge.ConfigForm.from_config(make_demo_config()).to_config()
    assert ok_result.ok
    assert not bridge.ConfigResult().ok


# --------------------------------------------------------------------------
# Qt 组件（offscreen）
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def qapp():
    """提供一个 QApplication 实例（整个模块共用）。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _silence_message_boxes(monkeypatch):
    """把所有模态对话框换成空实现。

    **这一步是必需的，不是图省事**：``QMessageBox`` 即使跑在 offscreen
    平台上也会进入自己的事件循环等用户点按钮 —— 测试会永久挂住
    （本步第一次跑 GUI 测试就挂死在 ``QMessageBox.information`` 上，
    300 秒超时被移到后台）。

    实测记录：``QMessageBox.question`` 的返回类型是 ``StandardButton``，
    替身必须返回同类型，否则调用方的比较会抛异常。
    """
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "about", lambda *a, **k: None)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *a, **k: QMessageBox.StandardButton.No,
    )


@pytest.fixture(autouse=True)
def _silence_file_dialogs(monkeypatch):
    """文件对话框同样会阻塞，一律换成「用户取消」。"""
    from PySide6.QtWidgets import QFileDialog

    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: ("", ""))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: ("", ""))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: "")


@pytest.fixture
def window(qapp):
    from stm32gen_gui.main_window import MainWindow

    win = MainWindow()
    win.on_new()  # 从空配置开始，避免示例数据干扰断言
    yield win
    win.close()


def test_window_builds(window) -> None:
    assert window.tabs.count() == 8
    assert [window.tabs.tabText(i) for i in range(8)] == [
        "全局",
        "GPIO",
        "AFIO",
        "EXTI",
        "USART",
        "TIM",
        "ADC",
        "NVIC",
    ]


ALL_GENERATED = [
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
]


def _preview_names(window) -> list[str]:
    return [window.preview_tabs.tabText(i) for i in range(window.preview_tabs.count())]


def test_window_preview_defaults_to_sources_only(window) -> None:
    """预览默认**只显示 .c**，.h 不占页签。

    理由：要读、要手改业务逻辑的是实现文件。13 个页签平铺时，
    找 bsp_usart.c 得先在 .h/.c 交错的一排里数一遍。

    注意这只是**显示**筛选 —— .h 照常生成、照常导出（见后面的测试）。
    """
    names = _preview_names(window)
    # 从 ALL_GENERATED 派生，新增外设时不必手改这份清单
    assert names == [name for name in ALL_GENERATED if not name.endswith(".h")]
    assert not any(name.endswith(".h") for name in names)


def test_window_preview_filter_shows_headers(window) -> None:
    """切到「仅 .h」分组时只显示头文件。"""
    window.preview_filter.setCurrentIndex(window.preview_filter.findData("headers"))
    names = _preview_names(window)
    assert names == [name for name in ALL_GENERATED if name.endswith(".h")]
    assert not any(name.endswith(".c") for name in names)


def test_window_preview_filter_all_shows_everything(window) -> None:
    """切到「全部」时要与生成器产物一一对应（13 个，按外设分文件）。"""
    window.preview_filter.setCurrentIndex(window.preview_filter.findData("all"))
    assert _preview_names(window) == ALL_GENERATED


def test_window_preview_filter_does_not_change_export_set(window) -> None:
    """筛选只影响显示，**不影响导出** —— .h 必须照常导出。

    这是本次改动最关键的一条：用户要的是"预览清爽"，不是"少给文件"。
    若筛选顺手改了导出集合，生成的工程会缺头文件而编译不过。
    """
    for group in ("sources", "headers", "all"):
        window.preview_filter.setCurrentIndex(window.preview_filter.findData(group))
        # _files 与 _generated 始终是全部 13 个产物
        assert len(window._files) == len(ALL_GENERATED)
        assert sorted(window._files) == sorted(ALL_GENERATED)
        assert [item.name for item in window._generated] == ALL_GENERATED


def test_window_preview_filter_keeps_current_tab(window) -> None:
    """切换分组时若当前文件在新分组里，要保留选中而不是跳回第一个。"""
    window.preview_filter.setCurrentIndex(window.preview_filter.findData("all"))
    target = window.preview_tabs.indexOf(
        next(
            window.preview_tabs.widget(i)
            for i in range(window.preview_tabs.count())
            if window.preview_tabs.tabText(i) == "bsp_gpio.c"
        )
    )
    window.preview_tabs.setCurrentIndex(target)

    # 切到仅 .h：bsp_gpio.c 不在其中，应回退到第一个（这是合理行为）
    window.preview_filter.setCurrentIndex(window.preview_filter.findData("headers"))
    assert window.preview_tabs.tabText(0) == "bsp_init.h"

    # 切回仅 .c：bsp_gpio.c 存在，但当前是 .h —— 回退到第一个 .c
    window.preview_filter.setCurrentIndex(window.preview_filter.findData("sources"))
    assert window.preview_tabs.tabText(0) == "bsp_init.c"


def test_window_copy_works_for_header(window, qapp) -> None:
    """「复制当前文件」在 .h 分组下也要能取到内容。

    ``on_copy`` 查的是 ``_files``（全部产物），所以切到 .h 分组复制
    bsp_gpio.h 拿到的应该是引脚宏所在的头文件内容。
    """
    from PySide6.QtWidgets import QApplication

    window.preview_filter.setCurrentIndex(window.preview_filter.findData("headers"))
    index = _preview_names(window).index("bsp_gpio.h")
    window.preview_tabs.setCurrentIndex(index)
    assert window.preview_tabs.tabText(index) == "bsp_gpio.h"
    window.on_copy()
    text = QApplication.clipboard().text()
    assert "#ifndef __BSP_GPIO_H" in text
    assert "void bsp_gpio_init(void);" in text


def test_window_loads_demo_config(qapp) -> None:
    from stm32gen_gui.main_window import MainWindow

    win = MainWindow()  # 构造函数内部载入示例配置
    assert win.gpio_table.rowCount() == 1
    assert win.usart_list.count() == 1
    assert win.nvic_table.rowCount() == 1
    assert "生成完成" in win.status_label.text()
    win.close()


def test_window_add_remove_gpio(window) -> None:
    assert window.gpio_table.rowCount() == 0

    window.on_add_gpio()
    assert window.gpio_table.rowCount() == 1
    assert "生成完成" in window.status_label.text()

    window.gpio_table.setCurrentCell(0, 0)
    window.on_remove_gpio()
    assert window.gpio_table.rowCount() == 0


def test_window_gpio_combo_has_options(window) -> None:
    window.on_add_gpio()
    mode_combo = window.gpio_table.cellWidget(0, 2)
    assert mode_combo is not None
    assert mode_combo.count() == len(GpioMode)
    # 数据是标准库符号名，不是显示名
    assert mode_combo.itemData(0).startswith("GPIO_Mode_")


# --------------------------------------------------------------------------
# GPIO 表：端口与引脚都是下拉框（选项由芯片封装推导）
# --------------------------------------------------------------------------


def test_window_port_column_is_combo_with_pin_counts(window) -> None:
    """端口列是下拉框，且标签带引脚数。

    用户不必翻数据手册就知道 PC 只有 3 个引脚。
    """
    window.on_add_gpio()
    port_combo = window.gpio_table.cellWidget(0, 0)
    assert port_combo is not None
    labels = [port_combo.itemText(i) for i in range(port_combo.count())]
    assert any("GPIOA" in label and "16 引脚" in label for label in labels)
    assert any("GPIOC" in label and "3 引脚" in label for label in labels)


def test_window_port_combo_excludes_missing_ports(window) -> None:
    """LQFP48 上没有 GPIOE/F/G，下拉里就不该出现。

    写死 A~G 的后果是用户能选中 GPIOE，然后只能等校验层报错 ——
    把「封装上不存在」留给用户去撞，是工具没尽责。
    """
    window.on_add_gpio()
    port_combo = window.gpio_table.cellWidget(0, 0)
    values = [port_combo.itemData(i) for i in range(port_combo.count())]
    assert values == ["GPIOA", "GPIOB", "GPIOC", "GPIOD"]


def test_window_pin_column_is_combo(window) -> None:
    """引脚列是下拉框，不是自由文本输入。"""
    window.on_add_gpio()
    pin_combo = window.gpio_table.cellWidget(0, 1)
    assert pin_combo is not None
    # 默认端口 GPIOA 有 16 个引脚
    assert pin_combo.count() == 16
    assert pin_combo.itemData(0) == "0"


def test_window_pin_combo_follows_port(window) -> None:
    """端口换成 GPIOC 后，引脚下拉只剩 13/14/15。"""
    window.on_add_gpio()
    window._set_combo(window.gpio_table.cellWidget(0, 0), "GPIOC")
    pin_combo = window.gpio_table.cellWidget(0, 1)
    values = [pin_combo.itemData(i) for i in range(pin_combo.count())]
    assert values == ["13", "14", "15"]


def test_window_pin_combo_keeps_same_number_across_ports(window) -> None:
    """换端口时能保留就保留引脚号。

    从 PA13 改到 PB13 不该让用户重新选一次引脚号。
    """
    window.on_add_gpio()
    window._set_combo(window.gpio_table.cellWidget(0, 1), "13")
    window._set_combo(window.gpio_table.cellWidget(0, 0), "GPIOB")
    assert window.gpio_table.cellWidget(0, 1).currentData() == "13"


def test_window_pin_combo_falls_back_when_number_missing(window) -> None:
    """换到没有该引脚号的端口时回退到第一个可用值，而不是留个空值。

    用 PA15 → GPIOD 构造：GPIOD 只有 PD0/PD1，没有 15。
    留空会让 ``GpioRow.pin`` 变成 ``""``，再报一个难懂的解析错误。

    （注意别用 PB15 → GPIOC：PC 有 13~15，15 是存在的，保留成功 ——
    这条测试初版就写错成那样，断言失败才发现。）"""
    window.on_add_gpio()
    window._set_combo(window.gpio_table.cellWidget(0, 0), "GPIOA")
    window._set_combo(window.gpio_table.cellWidget(0, 1), "15")
    window._set_combo(window.gpio_table.cellWidget(0, 0), "GPIOD")
    pin_combo = window.gpio_table.cellWidget(0, 1)
    assert pin_combo.currentData() == "0"
    assert pin_combo.currentData() is not None  # 绝不能留空


def test_window_pin_combo_marks_reserved_pins(window) -> None:
    """被晶振/调试口占用的引脚仍列出，但标注出来。

    它们在电气上确实可用（板子没焊晶振时 PD0 完全可以当普通 IO），
    直接隐藏反而会让用户以为工具漏了引脚 —— 取舍由用户做，
    风险由标注告知。
    """
    window.on_add_gpio()
    window._set_combo(window.gpio_table.cellWidget(0, 0), "GPIOD")
    pin_combo = window.gpio_table.cellWidget(0, 1)
    values = [pin_combo.itemData(i) for i in range(pin_combo.count())]
    labels = [pin_combo.itemText(i) for i in range(pin_combo.count())]
    assert values == ["0", "1"]  # 仍然可选
    assert "OSC_IN" in labels[0]
    assert "OSC_OUT" in labels[1]


def test_window_gpio_table_roundtrip_through_model(window) -> None:
    """下拉框选的引脚要能正确进模型 —— 这是本次改动的核心契约。"""
    window.on_add_gpio()
    window._set_combo(window.gpio_table.cellWidget(0, 0), "GPIOC")
    window._set_combo(window.gpio_table.cellWidget(0, 1), "13")
    window._set_combo(window.gpio_table.cellWidget(0, 4), "Bit_SET")

    config = window._form_to_config()
    assert len(config.gpio) == 1
    pin = config.gpio[0]
    assert pin.port.letter == "C"
    assert pin.pin == 13
    assert pin.init_level.value == "Bit_SET"


def test_window_add_usart(window) -> None:
    assert window.usart_list.count() == 0
    window.on_add_usart()
    assert window.usart_list.count() == 1
    # 添加后应自动刷新预览
    assert "生成完成" in window.status_label.text()


def test_window_add_usart_respects_limit(window) -> None:
    """本芯片只支持 3 个 USART，第 4 次添加应被拒绝。"""
    for _ in range(3):
        window.on_add_usart()
    assert window.usart_list.count() == 3
    # 第四次：弹窗提示，数量不变
    window.on_add_usart()
    assert window.usart_list.count() == 3


def test_window_usart_sync_nvic(window) -> None:
    """「从 USART 中断同步」要自动补齐 NVIC 通道。"""
    window.on_add_usart()
    # 给第一个 USART 勾上 RXNE 中断
    window.usart_interrupt_boxes["USART_IT_RXNE"].setChecked(True)
    assert window.nvic_table.rowCount() == 0

    window.on_sync_nvic()
    assert window.nvic_table.rowCount() == 1
    combo = window.nvic_table.cellWidget(0, 0)
    assert combo.currentData() == "USART1_IRQn"

    # 再同步一次不应重复添加
    window.on_sync_nvic()
    assert window.nvic_table.rowCount() == 1


def test_window_nvic_options_from_library(window) -> None:
    """中断号选项必须来自标准库 IRQn_Type（真实存在的中断号）。"""
    from stm32gen.stdlib_ref import default_index

    index = default_index()
    window.on_add_nvic()
    combo = window.nvic_table.cellWidget(0, 0)
    for row in range(combo.count()):
        name = combo.itemData(row)
        assert index.has_enum(name), f"{name} 不在标准库 IRQn_Type 里"


def test_window_irq_options_only_known(window) -> None:
    """下拉里的中断号必须都是库里真实存在的。"""
    from stm32gen.stdlib_ref import default_index

    index = default_index()
    for value, _ in window._irq_options():
        assert index.has_enum(value)
    # 本工具能配的中断只有 USART / EXTI / TIM / ADC 四类
    names = [value for value, _ in window._irq_options()]
    assert all(name.startswith(("USART", "EXTI", "TIM", "ADC")) for name in names)
    # ADC 必须在里面：ADC1/ADC2 共用的 ADC1_2_IRQn 若不在下拉里，
    # 同步 NVIC 时 _combo 会停在首项，把中断号静默写成别的通道。
    assert "ADC1_2_IRQn" in names


def test_window_generate_updates_preview(window) -> None:
    window.on_add_gpio()
    content = window._files.get("bsp_init.c", "")
    assert "void bsp_init(void)" in content


def test_window_copy_to_clipboard(window, qapp) -> None:
    from PySide6.QtWidgets import QApplication

    window.on_add_gpio()
    window.on_copy()
    text = QApplication.clipboard().text()
    assert "void bsp_init(void)" in text or "#ifndef" in text


def test_window_validate_action(window, qapp) -> None:
    """校验动作不该崩，且要刷新预览。"""
    window.on_validate()


def test_window_export_cancelled_is_safe(window) -> None:
    """用户在选择目录时取消，不该崩也不该写文件。"""
    window.on_add_gpio()
    window.on_export()  # 文件对话框被替身为返回空路径


def test_window_export_writes_all_files_regardless_of_filter(
    window, tmp_path, monkeypatch
) -> None:
    """导出必须写**全部 13 个文件**，与预览筛选无关。

    这是本次「GUI 里默认只显示 .c」改动最关键的一条守门测试。
    用户要的是预览清爽，不是少给文件 —— 若筛选顺手改了导出集合，
    生成的 Keil 工程会缺头文件而根本编译不过，且现象很隐蔽
    （只是在包含路径里找不到 .h）。

    这里走**真实的 on_export 路径**（只替身掉目录选择对话框），
    而不是直接断言 ``_files``，那样测不到写盘环节。
    """
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))

    # 故意停在「仅 .h」分组上
    window.preview_filter.setCurrentIndex(window.preview_filter.findData("headers"))
    window.on_add_gpio()
    window.on_export()

    written = sorted(path.name for path in tmp_path.iterdir())
    assert len(written) == len(ALL_GENERATED)
    assert "bsp_gpio.c" in written  # 不在当前预览分组里，但必须导出
    assert "bsp_gpio.h" in written
    assert "main.c" in written


def test_export_dialog_starts_in_the_output_dir(window, monkeypatch) -> None:
    """导出对话框的起始目录应指向源码树旁边的成果目录。

    工具与成果分开放：用户点「导出」时不该从源码树里开始找 —— 那里根本不该
    有产物，从那里起手很容易顺手选进 `src`，之后产物就进了 git。
    """
    from PySide6.QtWidgets import QFileDialog, QMessageBox
    from stm32gen import default_out_dir

    seen: list[tuple] = []

    def fake_dialog(*args, **kwargs):
        seen.append((args, kwargs))
        return ""  # 取消，不真的写盘

    monkeypatch.setattr(QFileDialog, "getExistingDirectory", fake_dialog)
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))

    window.on_export()

    assert len(seen) == 1, "对话框应当被调用一次"
    args, kwargs = seen[0]
    passed = list(args[2:3]) or [kwargs.get("dir")]
    assert passed == [default_out_dir()], (
        f"起始目录应为 {default_out_dir()!r}，实际 {passed!r}"
    )


def test_window_open_cancelled_is_safe(window) -> None:
    window.on_open()
    assert window._config_path is None


def test_window_save_without_path_goes_to_save_as(window) -> None:
    """没有文件路径时，「保存」应转成「另存为」（也走文件对话框）。"""
    window._config_path = None
    window.on_save()  # 另存为被取消，路径仍为空
    assert window._config_path is None


def test_window_about_does_not_crash(window) -> None:
    window.on_about()


# --------------------------------------------------------------------------
# 编译验证
# --------------------------------------------------------------------------


def test_window_compile_action(window) -> None:
    """「编译验证」要把结果写进问题面板，并在状态栏给结论。

    编译在**临时目录**里进行，不在用户目录留文件。

    标准库没配时这条要跳过：那种情况下 on_compile() 会走「编译验证不可用」
    分支提前返回（不弹 11 条找不到头文件的假错误），状态栏自然没有「通过」。
    """
    from stm32gen import configured_stdlib_root
    from stm32gen.compile_check import find_toolchain

    if find_toolchain() is None:
        pytest.skip("本机没有可用编译器")
    if configured_stdlib_root() is None:
        pytest.skip("标准库目录未配置（设置 STM32_STDLIB_ROOT 后才跑这条）")

    window.on_compile()
    panel = window.issue_panel.toPlainText()
    assert "编译器" in panel
    assert "结论" in panel
    assert "通过" in window.status_label.text()


def test_window_compile_refuses_without_stdlib(window, monkeypatch) -> None:
    """标准库没配时必须说「不可用」，不能说「编译失败」。

    这是防方向性错误：编译器缺头文件会报一串「标识符未定义」，如果照实显示
    成「编译失败」，用户会去查自己的配置，而真正要改的是环境变量。
    """
    from stm32gen.compile_check import find_toolchain

    toolchain = find_toolchain()
    if toolchain is None or not toolchain.kind.is_native:
        pytest.skip("本机没有 ARM 编译器")

    monkeypatch.setattr(
        "stm32gen_gui.main_window.configured_stdlib_root", lambda: None
    )
    shown: list[str] = []

    def _fake_information(parent, title, text, *args, **kwargs):
        shown.append(text)

    def _fake_critical(parent, title, text, *args, **kwargs):
        shown.append("CRITICAL:" + text)

    from PySide6.QtWidgets import QMessageBox as _Box

    monkeypatch.setattr(_Box, "information", _fake_information)
    monkeypatch.setattr(_Box, "critical", _fake_critical)

    window.on_compile()

    assert shown, "应当弹出提示"
    assert any("STM32_STDLIB_ROOT" in text for text in shown), shown
    assert not any(text.startswith("CRITICAL:") for text in shown), shown


def test_window_compile_warns_on_host_fallback(window, monkeypatch) -> None:
    """降级到宿主 gcc 时必须明确标注，不能让用户以为做了真实目标编译。

    用 monkeypatch 屏蔽 ARM 类工具链，而不是依赖本机没装 ——
    否则装了 Keil 这条测试就失去意义。
    """
    from stm32gen.compile_check import ToolchainKind, find_toolchain

    if find_toolchain(prefer=ToolchainKind.HOST) is None:
        pytest.skip("本机没有宿主 gcc")

    real_find = find_toolchain

    def only_host(prefer=None):
        """模拟「只有宿主 gcc」的环境。

        注意 ``prefer=None`` 是**默认调用**（`on_compile` 就是这么调的），
        必须单独处理：不能原样转发给 ``real_find``，否则 ARM 工具链照样
        会被返回。初版漏了这个分支，替身形同虚设。
        """
        if prefer is None:
            return real_find(prefer=ToolchainKind.HOST)
        if prefer.is_native:
            return None
        return real_find(prefer=prefer)

    monkeypatch.setattr("stm32gen.compile_check.find_toolchain", only_host)

    window.on_compile()
    panel = window.issue_panel.toPlainText()
    assert "宿主 gcc" in panel
    assert "ARM 专属问题" in panel


def test_window_compile_reports_keil(window) -> None:
    """有 Keil 时 GUI 的编译验证要用它，且标注为真实目标编译。"""
    from stm32gen import configured_stdlib_root
    from stm32gen.compile_check import find_toolchain

    found = find_toolchain()
    if found is None or not found.kind.is_keil:
        pytest.skip("本机没有 Keil 编译器")
    if configured_stdlib_root() is None:
        pytest.skip("标准库目录未配置（设置 STM32_STDLIB_ROOT 后才跑这条）")

    window.on_compile()
    panel = window.issue_panel.toPlainText()
    assert "Keil" in panel
    # 真实目标编译不该出现降级警告
    assert "ARM 专属问题" not in panel


def test_window_compile_no_toolchain(window, monkeypatch) -> None:
    """编译器缺失时明确说「不可用」，而不是报「失败」。

    两者对用户的意义不同：前者是环境问题（去装工具链），
    后者是代码问题（去改代码）。
    """
    monkeypatch.setattr("stm32gen.compile_check.find_toolchain", lambda: None)
    window.on_compile()
    assert "失败" not in window.status_label.text()


def test_window_compile_detects_broken_code(window, monkeypatch) -> None:
    """注入编译错误时，状态栏要说「编译失败」而不是「通过」。"""
    from stm32gen.compile_check import CompileResult, find_toolchain

    if find_toolchain() is None:
        pytest.skip("本机没有可用编译器")

    # check_sources 被整个替身掉，标准库根本不会被读；但 on_compile() 在调用
    # 它之前会先检查标准库是否配置，不配就直接弹「不可用」返回。这里给个假
    # 路径让流程走到替身，测试意图（注入编译错误）与标准库无关。
    monkeypatch.setattr(
        "stm32gen_gui.main_window.configured_stdlib_root", lambda: Path("/fake")
    )

    # 直接替身掉 check_sources，模拟编译失败
    def fake_check(config, directory, **kwargs):
        from stm32gen.compile_check import Diagnostic, Severity, Toolchain, ToolchainKind

        return CompileResult(
            toolchain=Toolchain(path="gcc", kind=ToolchainKind.HOST, version="test"),
            returncode=1,
            diagnostics=[
                Diagnostic(
                    file="bsp_init.c",
                    line=42,
                    column=1,
                    severity=Severity.ERROR,
                    message="'GPIO_Fake' undeclared",
                )
            ],
        )

    monkeypatch.setattr("stm32gen.compile_check.check_sources", fake_check)
    window.on_compile()

    assert "失败" in window.status_label.text()
    assert "GPIO_Fake" in window.issue_panel.toPlainText()


def test_window_invalid_form_shows_error_not_crash(window) -> None:
    """表单非法时要在问题面板里说明，且不弹窗打断输入。"""
    window.hse_edit.setText("abc")
    window.refresh_preview()
    assert "表单还有问题" in window.issue_panel.toPlainText()
    assert window.summary_label.text() == "表单有误"


def test_window_blocking_config_stops_generation(window) -> None:
    """有阻断性错误时预览要停止生成，并把问题面板切到错误清单。

    构造方式：加一个 PA9 GPIO，再加 USART1（TX 也是 PA9），制造引脚冲突。
    """
    window.on_add_gpio()
    window._set_combo(window.gpio_table.cellWidget(0, 0), "GPIOA")
    window._set_combo(window.gpio_table.cellWidget(0, 1), "9")
    window.on_add_usart()
    window.refresh_preview()

    panel = window.issue_panel.toPlainText()
    # 面板要给出错误计数与冲突详情
    assert "错误 1" in panel
    assert "PA9" in panel
    assert "多个输出驱动源" in panel

    # 预览必须切到错误页，而不是继续显示上一次的生成结果
    assert window.preview_tabs.tabText(0) == "错误"
    assert "阻断性错误" in window.preview_tabs.widget(0).toPlainText()
    assert window.status_label.text().startswith("存在阻断性错误")


def test_window_fixing_blocking_error_resumes_generation(window) -> None:
    """修好冲突后预览要恢复正常 —— 否则用户会以为工具坏了。"""
    window.on_add_gpio()
    window._set_combo(window.gpio_table.cellWidget(0, 0), "GPIOA")
    window._set_combo(window.gpio_table.cellWidget(0, 1), "9")
    window.on_add_usart()
    window.refresh_preview()
    assert window.preview_tabs.tabText(0) == "错误"

    # 把 GPIO 改成 PC13，冲突消失
    window._set_combo(window.gpio_table.cellWidget(0, 0), "GPIOC")
    window._set_combo(window.gpio_table.cellWidget(0, 1), "13")
    window.refresh_preview()

    assert window.preview_tabs.tabText(0) == "bsp_init.c"
    assert "生成完成" in window.status_label.text()


def test_window_highlighting_applied(window) -> None:
    """预览的编辑器上挂了高亮器（否则等于没高亮）。"""
    editor = window.preview_tabs.widget(1)  # bsp_init.c
    assert hasattr(editor, "highlighter")
    from stm32gen_gui.highlighter import CHighlighter

    assert isinstance(editor.highlighter, CHighlighter)


def test_window_config_path_tracking(window) -> None:
    """初始没有文件路径，「保存」应走「另存为」。"""
    assert window._config_path is None


def test_highlighter_handles_block_comments(qapp) -> None:
    """跨行块注释要能正确处理（不抛异常，状态正确传递）。"""
    from PySide6.QtGui import QTextDocument

    from stm32gen_gui.highlighter import CHighlighter

    doc = QTextDocument()
    doc.setPlainText("/**\n  * @brief 多行注释\n  * 第二行\n  */\nint main(void);\n")
    highlighter = CHighlighter(doc)
    highlighter.rehighlight()  # 触发全部行的高亮

    # 第 2 行（多行注释内部）结束时状态应为 1（仍在块注释里）
    block = doc.firstBlock()
    for _ in range(2):
        block = block.next()
    assert block.userState() == 1
    # 最后一行不该被当成注释
    last = doc.firstBlock()
    while last.next().isValid():
        last = last.next()
    assert last.userState() == 0


def test_highlighter_no_crash_on_generated_code(qapp, window) -> None:
    """对真实生成代码跑一遍高亮，不能抛异常。"""
    from PySide6.QtGui import QTextDocument

    from stm32gen_gui.highlighter import CHighlighter

    content = window._files.get("bsp_init.c", "")
    assert content
    doc = QTextDocument()
    doc.setPlainText(content)
    highlighter = CHighlighter(doc)
    highlighter.rehighlight()

# 下拉框右侧箭头与内边距占的宽度。列宽 = 文本宽 + 这个余量。
_COMBO_CHROME = 32


def test_exti_line_combo_shows_full_labels(window) -> None:
    """EXTI「线号」下拉必须能读全标签，不能截成「E…n」。

    回归测试：列宽曾只有 60px，而标签是 360px 宽的
    ``EXTI5 → EXTI9_5_IRQn（与 5~9 共享）`` —— 单元格里只剩「EX」，
    展开的弹层也同样是「E…n」。修法是缩短标签 + 加宽列 +
    完整通道名挂 tooltip。
    """
    from PySide6.QtGui import QFontMetrics

    from stm32gen_gui.main_window import EXTI_COLUMNS

    window.on_add_exti()
    combo = window.exti_table.cellWidget(0, 0)
    labels = [combo.itemText(i) for i in range(combo.count())]
    assert len(labels) == 16

    metrics = QFontMetrics(combo.font())
    needed = max(metrics.horizontalAdvance(label) for label in labels) + _COMBO_CHROME
    # 单元格放得下
    assert EXTI_COLUMNS[0][1] >= needed, (
        f"「线号」列宽 {EXTI_COLUMNS[0][1]}px 放不下最长标签（需要 {needed}px）"
    )
    # 展开的弹层也放得下（QComboBox 默认弹层与控件同宽，窄列会连带截断）
    assert combo.view().minimumWidth() >= needed


def test_exti_line_combo_tooltip_carries_irqn(window) -> None:
    """完整的中断通道名（EXTI9_5_IRQn 等）必须能在 tooltip 里看到。"""
    from PySide6.QtCore import Qt

    window.on_add_exti()
    combo = window.exti_table.cellWidget(0, 0)
    tooltips = {
        combo.itemText(i): combo.itemData(i, Qt.ItemDataRole.ToolTipRole)
        for i in range(combo.count())
    }
    assert "EXTI0_IRQn" in tooltips["EXTI0"]
    assert "EXTI9_5_IRQn" in tooltips["EXTI5（5~9）"]
    assert "EXTI15_10_IRQn" in tooltips["EXTI10（10~15）"]


# ── 布局：左右面板的空间分配（曾经右侧预览被挤成一条）─────────────────────


@pytest.fixture
def shown_window(qapp):
    """真正 show 出来的窗口。

    尺寸分配只在布局跑完之后才成立 —— 不 show 的话
    ``QSplitter.width()`` 还停在构造时的默认值，``sizes()`` 也是旧的，
    测出来的数字没有意义。
    """
    from stm32gen_gui.main_window import MainWindow

    win = MainWindow()
    win.resize(1440, 900)
    win.show()
    for _ in range(5):
        qapp.processEvents()
    win.on_new()
    for _ in range(5):
        qapp.processEvents()
    yield win
    win.close()


def test_no_tab_demands_more_than_700px(shown_window) -> None:
    """任何一页的最小宽度都不许超过 700px。

    回归测试：TIM 页曾把 7 个中断勾选框横排，光是它们就要 1080px，
    把整个左侧面板的最小宽度顶到 1310px（窗口总共才 1440px），
    右侧预览区于是被挤成 149px 的一条，分隔条也拖不动 —— 左侧已经
    到了它的最小宽度，再往里挤就违反约束。
    """
    window = shown_window
    widths = {
        window.tabs.tabText(i): window.tabs.widget(i).minimumSizeHint().width()
        for i in range(window.tabs.count())
    }
    too_wide = {name: width for name, width in widths.items() if width > 700}
    assert not too_wide, f"这些页的最小宽度超过 700px，会挤掉右侧预览区：{too_wide}"


def test_preview_gets_more_room_than_config_panel(shown_window) -> None:
    """默认分配下，右侧预览要比左侧配置区宽。

    预览区是读代码的地方，宽度不够就只剩换行和横向滚动条；
    配置区是操作区，窄一点无所谓。
    """
    left, right = shown_window.splitter.sizes()
    assert right >= 500, f"右侧预览只有 {right}px，扣掉页签和滚动条基本没法读代码"
    assert right > left, f"左右分配 {left}/{right} —— 预览区反而更窄"


def test_splitter_cannot_collapse_either_side(shown_window) -> None:
    """两侧都不许被拖成 0。

    折叠后配置区或预览区整个消失，用户很难自己意识到要再拖回来。
    """
    assert shown_window.splitter.childrenCollapsible() is False


# ── 左侧配置页 → 右侧预览的自动跳转 ────────────────────────────────


def _current_preview(window) -> str:
    tabs = window.preview_tabs
    return tabs.tabText(tabs.currentIndex())


@pytest.mark.parametrize(
    ("tab_index", "expected"),
    [
        (0, "bsp_init.c"),
        (1, "bsp_gpio.c"),
        (2, "bsp_afio.c"),
        (3, "bsp_exti.c"),
        (4, "bsp_usart.c"),
        (5, "bsp_tim.c"),
        (6, "bsp_adc.c"),
        (7, "bsp_nvic.c"),
    ],
)
def test_config_tab_switch_focuses_matching_source(window, tab_index, expected) -> None:
    """切到某个外设页，预览自动定位到它生成的 .c。"""
    window.tabs.setCurrentIndex(tab_index)
    assert _current_preview(window) == expected


def test_config_tab_sources_match_tab_order(window) -> None:
    """映射表的长度必须与页签数一致。

    加一个新外设页却忘了补 ``CONFIG_TAB_SOURCES`` 时，这里会先报错，
    而不是等用户在那一页发现预览没有跟着跳。
    """
    from stm32gen_gui.main_window import CONFIG_TAB_SOURCES

    assert len(CONFIG_TAB_SOURCES) == window.tabs.count()


def test_config_tab_switch_keeps_file_when_target_hidden(window) -> None:
    """筛选「仅 .h」时没有 .c 可跳，就不该动用户正在看的文件。"""
    window.preview_filter.setCurrentIndex(1)  # 仅 .h
    before = _current_preview(window)
    assert before.endswith(".h")

    window.tabs.setCurrentIndex(3)  # EXTI
    assert _current_preview(window) == before


def test_preview_tab_click_is_not_overridden(window) -> None:
    """用户自己点预览页签时不能被跳回去 —— 只有切左侧页签才自动定位。"""
    window.tabs.setCurrentIndex(1)  # GPIO → bsp_gpio.c
    assert _current_preview(window) == "bsp_gpio.c"

    window.preview_tabs.setCurrentIndex(0)  # 用户手动点回第一个
    assert _current_preview(window) == "bsp_init.c"


# --------------------------------------------------------------------------
# TIM 定时参数自动反算（bridge + GUI）
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind", "value"),
    [
        ("1000", "frequency", 1000.0),
        ("20kHz", "frequency", 20_000.0),
        ("1KHz", "frequency", 1000.0),
        ("20ms", "period", 0.02),
        ("0.5s", "period", 0.5),
        ("2.5s", "period", 2.5),
    ],
)
def test_parse_update_target_accepts_units(text, kind, value) -> None:
    """允许单位后缀：光看「20000」分不清 20kHz 还是 20Hz。"""
    parsed_kind, parsed = bridge.parse_update_target(text)
    assert parsed_kind == kind
    assert parsed == pytest.approx(value)


@pytest.mark.parametrize("text", ["", "   ", "abc", "1.2.3kHz", "10furlongs"])
def test_parse_update_target_rejects_garbage(text) -> None:
    with pytest.raises(ValueError):
        bridge.parse_update_target(text)


def test_timer_clock_is_double_pclk() -> None:
    """定时器时钟是 APB 分频不为 1 时的 2×PCLK，不是 PCLK 本身。

    默认 RCC 是 72MHz / APB1÷2 → PCLK1 36MHz → TIM2 时钟仍是 72MHz。
    这条最容易算错一倍，所以单独钉住。
    """
    assert bridge.timer_clock_hz("TIM2") == 72_000_000
    assert bridge.timer_clock_hz("TIM1") == 72_000_000


def test_solve_tim_timing_uses_timer_clock() -> None:
    """反算取的是定时器时钟；若误用 PCLK1(36MHz) 频率会差一倍。"""
    solution = bridge.solve_tim_timing("TIM2", "1kHz")
    assert solution.timer_clock_hz == 72_000_000
    assert solution.actual_hz == pytest.approx(1000)
    assert solution.prescaler == 1


def test_tim_timing_hint_reports_actual_frequency(window) -> None:
    """GUI 必须回显实际频率 —— 反算之后用户手改 PSC 仍然看得见后果。"""
    window.on_add_tim()
    window.tim_prescaler_edit.setText("71")
    window.tim_period_edit.setText("999")
    window._on_tim_edited()
    text = window.tim_timing_label.text()
    assert "1kHz" in text
    assert "72MHz" in text


def test_tim_timing_hint_survives_invalid_input(window) -> None:
    """输入框里出现半截数字（打字过程）不能弹错误，只能暂时不回显。"""
    window.on_add_tim()
    window.tim_prescaler_edit.setText("")
    window._on_tim_edited()
    assert window.tim_timing_label.text() == ""


def test_on_solve_tim_timing_fills_psc_arr(window) -> None:
    """点「自动计算」要把 PSC/ARR 真的填进控件，而不只是显示建议。"""
    window.on_add_tim()
    window.tim_instance_combo.setCurrentIndex(
        window.tim_instance_combo.findData("TIM2")
    )
    window.tim_target_edit.setText("20ms")
    window.on_solve_tim_timing()

    assert window.tim_period_edit.text() != "999"  # 已被覆盖
    solution = bridge.solve_tim_timing("TIM2", "20ms")
    assert window.tim_prescaler_edit.text() == str(solution.prescaler)
    assert window.tim_period_edit.text() == str(solution.period)
    assert "50Hz" in window.tim_timing_label.text()


def test_on_solve_tim_timing_reports_bad_unit(window) -> None:
    """写错单位时给出可读原因，不能静默不动。"""
    window.on_add_tim()
    window.tim_target_edit.setText("快到看不清")
    window.on_solve_tim_timing()
    assert "无法计算" in window.tim_timing_label.text()


def test_on_solve_tim_timing_rejects_too_fast(window) -> None:
    window.on_add_tim()
    window.tim_target_edit.setText("100MHz")
    window.on_solve_tim_timing()
    assert "更高" in window.tim_timing_label.text() or "无法计算" in window.tim_timing_label.text()


def test_tim_target_cleared_when_switching_instance(window) -> None:
    """切实例后清空期望输入 —— 否则会误以为新实例也按那个目标反算过。"""
    window.on_add_tim()
    window.on_add_tim()  # 第二个实例，列表选中它
    window.tim_target_edit.setText("20ms")
    window.tim_list.setCurrentRow(0)  # 切回第一个实例 → 触发 _fill_tim_form
    assert window.tim_target_edit.text() == ""


def test_solve_respects_center_aligned_mode(window) -> None:
    """中央对齐下同样的目标频率需要**一半**的总分频。

    中央对齐一个完整周期上下各数一遍。72MHz 求 1kHz 时边沿对齐要
    总分频 72000（PSC=1/ARR=35999），中央对齐只要 36000
    （PSC=0/ARR=35999）—— 差别落在 PSC 上，ARR 两边都会被撑到最大，
    因为反算优先保证占空比档位。
    """
    window.on_add_tim()
    window.tim_target_edit.setText("1kHz")
    window.on_solve_tim_timing()
    edge_psc = int(window.tim_prescaler_edit.text())
    edge_arr = int(window.tim_period_edit.text())

    window._set_combo(window.tim_counter_combo, "TIM_CounterMode_CenterAligned1")
    window.tim_target_edit.setText("1kHz")
    window.on_solve_tim_timing()
    center_psc = int(window.tim_prescaler_edit.text())
    center_arr = int(window.tim_period_edit.text())

    assert center_psc < edge_psc, "中央对齐应当需要更小的分频"
    assert center_arr == edge_arr, "两者都应把 ARR 撑到最大以保证占空比档位"
    assert "÷2（中央对齐）" in window.tim_timing_label.text()


def test_solve_uses_configured_hse_not_hardcoded_8mhz(window) -> None:
    """反算必须用全局页填的晶振频率。

    写死 8MHz 时的后果：用户填 12MHz 晶振，定时器时钟其实是 108MHz，
    但反算仍按 72MHz 算 → PSC/ARR 差 1.5 倍。更糟的是生成的代码取自
    配置里的时钟树（正确），于是「算的时候一套、跑起来另一套」。
    """
    window.on_add_tim()
    window.hse_edit.setText("12000000")
    assert window._hse_hz() == 12_000_000

    window.tim_target_edit.setText("1kHz")
    window.on_solve_tim_timing()
    expected = bridge.solve_tim_timing("TIM2", "1kHz", hse_hz=12_000_000)
    assert window.tim_prescaler_edit.text() == str(expected.prescaler)
    assert window.tim_period_edit.text() == str(expected.period)

    # 回显也要反映 108MHz，而不是停留在 72MHz
    assert "108MHz" in window.tim_timing_label.text()


def test_timing_hint_falls_back_to_8mhz_on_bad_hse(window) -> None:
    """晶振输入框里是半截数字时不能崩，退回默认值继续回显。"""
    window.on_add_tim()
    window.hse_edit.setText("")
    assert window._hse_hz() == 8_000_000
    assert "72MHz" in window.tim_timing_label.text()


# --------------------------------------------------------------------------
# 布局切换：分文件 / 合并
# --------------------------------------------------------------------------


def test_layout_combo_defaults_to_split(window) -> None:
    """默认仍是分文件布局，老用户看不到变化。"""
    from stm32gen.generator.layout import OutputLayout

    assert window.current_layout() is OutputLayout.SPLIT


def test_layout_switch_regenerates_preview(window) -> None:
    """切到合并后预览只剩 3 个页签，且都有内容。"""
    from stm32gen.generator.layout import OutputLayout

    window.layout_combo.setCurrentIndex(
        window.layout_combo.findData(OutputLayout.MERGED.value)
    )
    assert window.current_layout() is OutputLayout.MERGED
    names = [window.preview_tabs.tabText(i) for i in range(window.preview_tabs.count())]
    assert sorted(names) == ["bsp.c", "main.c"]


def test_layout_switch_back_restores_split(window) -> None:
    """来回切换不能留下上一轮的残留页签。"""
    from stm32gen.generator.layout import OutputLayout

    window.layout_combo.setCurrentIndex(
        window.layout_combo.findData(OutputLayout.MERGED.value)
    )
    window.layout_combo.setCurrentIndex(
        window.layout_combo.findData(OutputLayout.SPLIT.value)
    )
    names = [window.preview_tabs.tabText(i) for i in range(window.preview_tabs.count())]
    assert "bsp_gpio.c" in names
    assert "bsp.h" not in names


def test_merged_layout_export_writes_three_files(window, tmp_path) -> None:
    """合并布局下导出只落 3 个文件（走真实 on_export）。"""
    from unittest.mock import patch

    from stm32gen.generator.layout import OutputLayout

    window.layout_combo.setCurrentIndex(
        window.layout_combo.findData(OutputLayout.MERGED.value)
    )
    with patch(
        "stm32gen_gui.main_window.QFileDialog.getExistingDirectory",
        return_value=str(tmp_path),
    ), patch("stm32gen_gui.main_window.QMessageBox.information"):
        window.on_export()

    assert sorted(path.name for path in tmp_path.glob("*")) == ["bsp.c", "bsp.h", "main.c"]


def test_merged_layout_focuses_bsp_c(window) -> None:
    """合并布局下切配置页都跳到 bsp.c —— 那确实是全部实现所在。"""
    from stm32gen.generator.layout import OutputLayout

    window.layout_combo.setCurrentIndex(
        window.layout_combo.findData(OutputLayout.MERGED.value)
    )
    window.tabs.setCurrentIndex(3)  # EXTI
    current = window.preview_tabs.tabText(window.preview_tabs.currentIndex())
    assert current == "bsp.c"

# --------------------------------------------------------------------------
# 启动与自检
# --------------------------------------------------------------------------


def test_selfcheck_survives_gbk_console() -> None:
    """自检在 GBK 控制台下不能崩。

    这是真出过的问题：自检里打了一个 ``↔``（U+2194），GBK 没有这个码位，
    输出重定向时整个自检抛 UnicodeEncodeError —— 用户看到的是「GUI 启动
    不了」，实际只是某个字符打不出来。

    用子进程跑，因为 ``PYTHONIOENCODING`` 只在新进程里才能真正生效；
    进程内改 ``sys.stdout`` 测不到 ``print`` 到原始缓冲区的那条路径。
    """
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "gbk"
    env["QT_QPA_PLATFORM"] = "offscreen"
    proc = subprocess.run(
        [sys.executable, "-m", "stm32gen_gui", "--selfcheck"],
        capture_output=True,
        env=env,
        timeout=120,
    )
    stderr = proc.stderr.decode("gbk", errors="replace")
    stdout = proc.stdout.decode("gbk", errors="replace")
    assert "UnicodeEncodeError" not in stderr
    assert "Traceback" not in stderr, stderr
    assert proc.returncode == 0, stdout + stderr
    # 三项检查都要真的跑到，不能靠提前崩掉「恰好没报错」
    assert "[ OK ] 核心库" in stdout
    assert "[ OK ] 桥接层" in stdout
    assert "[ OK ] 核心资源" in stdout
    assert "[ OK ] Qt 平台插件" in stdout


def test_selfcheck_reports_each_step() -> None:
    """自检的三步一项都不能少 —— 少一项就等于没检查那一层。"""
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    # 必须钉住子进程的输出编码：不设的话它按系统代码页（简中为 GBK）写，
    # 这里再按 utf-8 解码就把中文全变成替换符，断言莫名其妙地失败。
    # 曾经只在单独跑时通过 —— 因为那时外层继承了 PYTHONIOENCODING。
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-m", "stm32gen_gui", "--selfcheck"],
        capture_output=True,
        env=env,
        timeout=120,
    )
    out = proc.stdout.decode("utf-8", errors="replace")
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    for step in ("核心库", "桥接层", "核心资源", "Qt 平台插件"):
        assert step in out, f"自检输出缺少「{step}」，实际输出：\n{out}"

def test_window_has_application_icon(window) -> None:
    """窗口要带自己的图标，否则任务栏和 Alt+Tab 里是个通用白框。

    图标放在包内（``stm32gen_gui/assets``）而不是项目根，这样它跟着安装走。
    ``_app_icon()`` 在文件缺失时返回空 QIcon 而不抛异常 —— 所以这里断言
    "非空"，一旦哪天路径写错就会红，而不是悄悄退化成默认图标。
    """
    icon = window.windowIcon()
    assert not icon.isNull()
    sizes = icon.availableSizes()
    assert sizes, "窗口图标没有任何可用尺寸"
    assert any(size.width() >= 32 for size in sizes), "图标缺少大尺寸，高分屏上会糊"


def test_app_icon_file_exists_in_package() -> None:
    """图标文件必须在包里 —— 这是 ``_app_icon()`` 唯一的依赖。"""
    from stm32gen_gui.main_window import _app_icon

    assert not _app_icon().isNull()

# --------------------------------------------------------------------------
# NVIC：中断号选项必须覆盖 TIM
# --------------------------------------------------------------------------


def test_irq_options_include_tim(window) -> None:
    """中断号下拉里必须有 TIM 的中断号。

    这是用户报过的问题：「NVIC 里没有配 TIM2_IRQn 通道」。原因不在校验层，
    而在 ``_irq_options()`` 只列了 USART 与 EXTI 的前缀 —— 用户根本选不到
    TIM 的 IRQn，只能手填（而这一列是下拉框，填不进去）。
    """
    values = [value for value, _ in window._irq_options()]
    for irqn in ("TIM1_UP_IRQn", "TIM2_IRQn", "TIM3_IRQn", "TIM4_IRQn"):
        assert irqn in values, f"{irqn} 不在中断号选项里"


def test_irq_options_cover_all_three_peripherals(window) -> None:
    """USART / EXTI / TIM 三类都要在，一个都不能漏。"""
    values = [value for value, _ in window._irq_options()]
    for prefix in ("USART", "EXTI", "TIM"):
        assert any(v.startswith(prefix) for v in values), f"缺 {prefix} 类中断号"


def test_irq_options_do_not_invent_symbols(window) -> None:
    """选项必须全部来自标准库 IRQn_Type，不能有编造的名字。

    TIM5_IRQn / TIM6_IRQn / TIM7_IRQn 在中容量型号上不存在 —— 如果哪天有人
    "顺手"把它们写进清单，这条会红。
    """
    from stm32gen.stdlib_ref import default_index

    real = set(default_index().enum_values("IRQn_Type"))
    for value, _ in window._irq_options():
        assert value in real, f"{value} 不是标准库里真实存在的中断号"


def test_sync_tim_nvic_adds_channel(window) -> None:
    """「从 TIM 中断同步」要能把勾了中断的 TIM 补进 NVIC。"""
    from PySide6.QtCore import Qt

    for row in range(window.nvic_table.rowCount() - 1, -1, -1):
        window.nvic_table.removeRow(row)

    window.on_add_tim()
    form = window.tim_list.item(0).data(Qt.ItemDataRole.UserRole)
    form.interrupts = ["TIM_IT_Update"]
    window.tim_list.item(0).setData(Qt.ItemDataRole.UserRole, form)

    window.on_sync_tim_nvic()
    assert window.nvic_table.rowCount() == 1
    assert window.nvic_table.cellWidget(0, 0).currentData() == "TIM1_UP_IRQn"
    assert "TIM1" in window.status_label.text() or "新增 1" in window.status_label.text()


def test_sync_tim_nvic_uses_timx_irqn_for_tim2(window) -> None:
    """TIM2 必须是 TIM2_IRQn，不能套用 TIM1 的 TIM1_UP_IRQn。

    高级定时器与通用定时器的向量名不同：TIM1 的更新事件走 TIM1_UP_IRQn，
    TIM2~4 各只有一个 TIMx_IRQn。写错的话 ISR 名字与启动文件对不上，
    中断进来会跑进 Default_Handler 死循环。
    """
    from PySide6.QtCore import Qt

    for row in range(window.nvic_table.rowCount() - 1, -1, -1):
        window.nvic_table.removeRow(row)

    window.on_add_tim()
    form = window.tim_list.item(0).data(Qt.ItemDataRole.UserRole)
    form.instance = "TIM2"
    form.interrupts = ["TIM_IT_Update"]
    window.tim_list.item(0).setData(Qt.ItemDataRole.UserRole, form)

    window.on_sync_tim_nvic()
    assert window.nvic_table.cellWidget(0, 0).currentData() == "TIM2_IRQn"


def test_sync_tim_nvic_adds_one_row_per_instance(window) -> None:
    """一个 TIM 只加一行 —— 勾了多个中断也是同一条 NVIC 通道。

    逐条中断加行会插出重复的 IRQn，校验层会报「多个通道配了同一个中断号」，
    而且 NVIC_Init 对同一通道配两次没有意义。
    """
    from PySide6.QtCore import Qt

    for row in range(window.nvic_table.rowCount() - 1, -1, -1):
        window.nvic_table.removeRow(row)

    window.on_add_tim()
    form = window.tim_list.item(0).data(Qt.ItemDataRole.UserRole)
    # CC1/CC2 中断要求对应通道已存在，否则模型层直接拒绝（这是刻意的校验），
    # 这里把通道补齐，让这条测试只考察「同步按钮一次只加一行」。
    from stm32gen_gui import bridge

    form.channels = [
        bridge.TimChannelRow(channel="TIM_Channel_1", pulse="500"),
        bridge.TimChannelRow(channel="TIM_Channel_2", pulse="500"),
    ]
    form.interrupts = ["TIM_IT_Update", "TIM_IT_CC1", "TIM_IT_CC2"]
    window.tim_list.item(0).setData(Qt.ItemDataRole.UserRole, form)

    window.on_sync_tim_nvic()
    assert window.nvic_table.rowCount() == 1


def test_sync_tim_nvic_is_idempotent(window) -> None:
    """连点两次同步不能插出重复通道。"""
    from PySide6.QtCore import Qt

    for row in range(window.nvic_table.rowCount() - 1, -1, -1):
        window.nvic_table.removeRow(row)

    window.on_add_tim()
    form = window.tim_list.item(0).data(Qt.ItemDataRole.UserRole)
    form.interrupts = ["TIM_IT_Update"]
    window.tim_list.item(0).setData(Qt.ItemDataRole.UserRole, form)

    window.on_sync_tim_nvic()
    window.on_sync_tim_nvic()
    assert window.nvic_table.rowCount() == 1
    assert "无需同步" in window.status_label.text()

# --------------------------------------------------------------------------
# 打包配置
# --------------------------------------------------------------------------


def test_packaging_spec_sets_exe_icon() -> None:
    """spec 必须给 EXE 传 icon。

    不传的话 PyInstaller 塞它自己的「文件夹+箭头」，与窗口图标不一致：
    窗口里是我们画的芯片，资源管理器里是 PyInstaller 的默认图标。
    两者是**两套机制** —— 窗口图标靠运行时 ``setWindowIcon``，
    exe 图标靠打包时嵌入资源，缺一不可。
    """
    spec = Path(__file__).parent.parent / "packaging" / "stm32initgen.spec"
    assert spec.is_file(), f"找不到打包配置：{spec}"
    text = spec.read_text(encoding="utf-8")
    assert "icon=" in text, "spec 的 EXE 没有 icon 参数"
    assert "stm32-init-gen.ico" in text


def test_packaging_spec_does_not_break_console_default() -> None:
    """发布版必须是 console=False，否则多一个黑窗口。"""
    spec = Path(__file__).parent.parent / "packaging" / "stm32initgen.spec"
    text = spec.read_text(encoding="utf-8")
    assert "console=False" in text

# --------------------------------------------------------------------------
# TIM 外部时钟的界面
# --------------------------------------------------------------------------


def test_clock_source_combo_lists_all_sources(window) -> None:
    """时钟源下拉要覆盖内部与三种外部模式。"""
    window.on_add_tim()
    values = [
        window.tim_clock_source_combo.itemData(i)
        for i in range(window.tim_clock_source_combo.count())
    ]
    assert "" in values, "缺内部时钟"
    assert "TIM_ETRClockMode1Config" in values
    assert "TIM_TxExternalCLK1Source_TI1".replace("Tx", "TIx") or True
    assert "TIM_TIxExternalCLK1Source_TI1" in values
    assert "TIM_TS_ITR0" in values


def test_external_clock_fields_hidden_for_internal(window) -> None:
    """内部时钟下 ETR 参数整组隐藏，避免摆一堆不生效的输入框。"""
    window.on_add_tim()
    window.tim_clock_source_combo.setCurrentIndex(
        window.tim_clock_source_combo.findData("")
    )
    # 用 isHidden() 而不是 isVisible()：窗口没 show 时 isVisible() 恒为 False，
    # 测不出"我们是否主动隐藏了它"。
    assert window.tim_etr_prescaler_combo.isHidden()
    assert window.tim_external_clock_edit.isHidden()


def test_external_clock_disables_auto_solve(window) -> None:
    """外部时钟下「期望更新」与自动计算必须禁用。

    工具不知道外部信号频率，反算出来的 PSC/ARR 一定错 —— 与其给出
    一个错答案，不如明确要求用户自己填。
    """
    window.on_add_tim()
    window.tim_clock_source_combo.setCurrentIndex(
        window.tim_clock_source_combo.findData("TIM_ETRClockMode1Config")
    )
    assert not window.tim_target_edit.isEnabled()
    assert not window.tim_solve_button.isEnabled()
    assert not window.tim_external_clock_edit.isHidden()


def test_external_clock_hint_says_frequency_is_unknown(window) -> None:
    """没填外部频率时回显必须说「未知」，不能退回时钟树算一个数。"""
    window.on_add_tim()
    window.tim_clock_source_combo.setCurrentIndex(
        window.tim_clock_source_combo.findData("TIM_ETRClockMode1Config")
    )
    text = window.tim_timing_label.text()
    assert "外部" in text
    assert "未知" in text
    assert "72MHz" not in text


def test_external_clock_hint_computes_with_frequency(window) -> None:
    """填了外部频率就给出推算，并计入 ETR 预分频。"""
    window.on_add_tim()
    window.tim_clock_source_combo.setCurrentIndex(
        window.tim_clock_source_combo.findData("TIM_ETRClockMode1Config")
    )
    window.tim_etr_prescaler_combo.setCurrentIndex(
        window.tim_etr_prescaler_combo.findData("TIM_ExtTRGPSC_DIV4")
    )
    window.tim_prescaler_edit.setText("0")
    window.tim_period_edit.setText("99")
    window.tim_external_clock_edit.setText("1000")
    window._refresh_tim_timing_hint()
    text = window.tim_timing_label.text()
    # 1000 ÷4 = 250Hz 计数；÷100 = 2.5Hz 更新
    assert "250Hz" in text
    assert "2.5Hz" in text


def test_tim_form_roundtrips_external_clock(window) -> None:
    """外部时钟参数要能存进列表项再读回来。"""
    from PySide6.QtCore import Qt

    window.on_add_tim()
    window.tim_clock_source_combo.setCurrentIndex(
        window.tim_clock_source_combo.findData("TIM_ETRClockMode1Config")
    )
    window.tim_etr_prescaler_combo.setCurrentIndex(
        window.tim_etr_prescaler_combo.findData("TIM_ExtTRGPSC_DIV8")
    )
    window.tim_etr_filter_edit.setText("7")
    window.tim_external_clock_edit.setText("2000")
    window._on_tim_edited()

    form = window.tim_list.item(0).data(Qt.ItemDataRole.UserRole)
    assert form.clock_source == "TIM_ETRClockMode1Config"
    assert form.etr_prescaler == "TIM_ExtTRGPSC_DIV8"
    assert form.etr_filter == "7"
    assert form.external_clock_hz == "2000"


def test_tim_list_shows_external_clock_marker(window) -> None:
    """列表行要标出外部时钟 —— 否则 PSC/ARR 看着一样但含义完全不同。"""
    window.on_add_tim()
    window.tim_clock_source_combo.setCurrentIndex(
        window.tim_clock_source_combo.findData("TIM_ETRClockMode1Config")
    )
    window._on_tim_edited()
    text = window.tim_list.item(0).text()
    assert "外部时钟" in text

# --------------------------------------------------------------------------
# ADC 页
# --------------------------------------------------------------------------


def test_adc_form_roundtrips_all_fields() -> None:
    """bridge 层的表单往返：填表 → 模型 → 再读回，一个字都不能丢。

    这是 bridge 保持**不依赖 Qt** 的价值所在：这段逻辑可以无头单测，
    而界面上最容易出错的正是这层双向转换。
    """
    form = bridge.AdcForm(
        instance="ADC1",
        mode="ADC_Mode_Independent",
        prescaler="RCC_PCLK2_Div6",
        scan_mode=True,
        continuous=True,
        trigger="ADC_ExternalTrigConv_None",
        data_align="ADC_DataAlign_Right",
        interrupts=["ADC_IT_EOC"],
        watchdog=True,
        watchdog_channel="ADC_Channel_9",
        watchdog_high="3000",
        watchdog_low="100",
        calibration=True,
        label="主采样",
        channels=[
            bridge.AdcChannelRow(
                channel="ADC_Channel_3",
                sample_time="ADC_SampleTime_28Cycles5",
                port="GPIOA",
                pin="3",
                label="电位器",
            )
        ],
    )
    config = form.to_config()
    assert config.instance.value == "ADC1"
    assert config.sequence_length == 1
    assert config.watchdog_high == 3000
    assert config.watchdog_low == 100

    back = bridge.AdcForm.from_config(config)
    assert back.instance == form.instance
    assert back.prescaler == form.prescaler
    assert back.continuous is True
    assert back.watchdog is True
    assert back.watchdog_channel == "ADC_Channel_9"
    assert back.channels[0].port == "GPIOA"
    assert back.channels[0].pin == "3"
    assert back.channels[0].label == "电位器"


def test_adc_internal_channel_drops_pin_text() -> None:
    """内部通道（16/17）不接引脚，表格里残留的端口/引脚文本不能带进模型。

    否则模型层会报「内部通道不接任何引脚」，而用户根本没填过 ——
    那是切换通道时留下的脏数据。
    """
    row = bridge.AdcChannelRow(
        channel="ADC_Channel_16", port="GPIOA", pin="1", label="温度"
    )
    config = row.to_config()
    assert config.port is None
    assert config.pin is None
    assert config.is_internal


def test_adc_watchdog_off_clears_stale_thresholds() -> None:
    """没开看门狗时，残留的阈值不能进模型。

    GUI 里用户很可能先填了阈值再取消勾选；若原样传给模型，
    会被「没开看门狗却填了阈值」的规则拦下 —— 那是噪音，不是真错误。
    """
    form = bridge.AdcForm(
        watchdog=False,
        watchdog_high="2000",
        watchdog_low="50",
        watchdog_channel="ADC_Channel_3",
        channels=[bridge.AdcChannelRow(channel="ADC_Channel_0")],
    )
    config = form.to_config()
    assert config.watchdog is False
    assert config.watchdog_high == 4095
    assert config.watchdog_low == 0
    assert config.watchdog_channel is None


def test_adc_trigger_options_exclude_adc3_only() -> None:
    """C8T6 上没有 ADC3，6 个 ADC3 专属触发源不该出现在下拉里。"""
    options = bridge.adc_trigger_options(has_adc3=False)
    values = {value for value, _ in options}
    assert "ADC_ExternalTrigConv_T5_CC1" not in values
    assert "ADC_ExternalTrigConv_T1_CC1" in values
    assert len(options) == 8
    # 大容量芯片上全部 14 个都可用
    assert len(bridge.adc_trigger_options(has_adc3=True)) == 14


def test_adc_prescaler_options_warn_on_overclock() -> None:
    """下拉标签要标出超 14MHz 的选项 —— 库的断言不查频率，只能在这里提醒。"""
    options = dict(bridge.adc_prescaler_options(72_000_000))
    assert "超 14MHz" in options["RCC_PCLK2_Div2"]
    assert "超 14MHz" in options["RCC_PCLK2_Div4"]
    assert "超 14MHz" not in options["RCC_PCLK2_Div6"]


def test_adc_channel_options_mark_internal_channels() -> None:
    """通道 16/17 的标签要说清是内部通道 —— 它们不占引脚。"""
    options = dict(bridge.adc_channel_options())
    assert "内部温度传感器" in options["ADC_Channel_16"]
    assert "Vrefint" in options["ADC_Channel_17"]
    assert "内部" not in options["ADC_Channel_3"]


def test_adc_tab_can_be_built(window) -> None:
    """ADC 页签能建出来，且配套控件齐全。"""
    assert window.adc_list is not None
    assert window.adc_channel_table is not None
    assert window.adc_instance_combo is not None
    assert len(window.adc_interrupt_boxes) == 3


def test_adc_add_creates_one_channel_by_default(window) -> None:
    """新增实例时默认带一条通道。

    空序列在模型层会被拒绝，GUI 不该造出一个必然报错的初始状态 ——
    用户点「添加」就应该看到一个能立刻生成的配置。
    """
    window.on_add_adc()
    assert window.adc_list.count() == 1
    assert window.adc_channel_table.rowCount() == 1


def test_adc_add_picks_unused_instance(window) -> None:
    """第二次添加应选还没用过的 ADC2。"""
    window.on_add_adc()
    window.on_add_adc()
    from PySide6.QtCore import Qt

    instances = [
        window.adc_list.item(row).data(Qt.ItemDataRole.UserRole).instance
        for row in range(window.adc_list.count())
    ]
    assert instances == ["ADC1", "ADC2"]


def test_adc_channel_dropdown_disables_pin_for_internal(window) -> None:
    """选内部通道时端口/引脚两格要禁用，避免用户白填。"""
    window.on_add_adc()
    window.adc_channel_table.cellWidget(0, 0).setCurrentIndex(
        window.adc_channel_table.cellWidget(0, 0).findData("ADC_Channel_16")
    )
    port_combo = window.adc_channel_table.cellWidget(0, 2)
    assert not port_combo.isEnabled()


def test_adc_channel_pin_enabled_for_external(window) -> None:
    window.on_add_adc()
    combo = window.adc_channel_table.cellWidget(0, 0)
    combo.setCurrentIndex(combo.findData("ADC_Channel_3"))
    assert window.adc_channel_table.cellWidget(0, 2).isEnabled()


def test_adc_watchdog_widgets_hide_with_label(window) -> None:
    """看门狗的字段隐藏时**必须连 QFormLayout 的标签一起隐藏**。

    窗口没 show 时 ``isVisible()`` 恒为 False，所以测隐藏要用 ``isHidden()``
    —— 否则这条测试永远通过，等于没写。
    """
    window.on_add_adc()
    label = window.adc_watchdog_form.labelForField(window.adc_watchdog_high_edit)
    assert label is not None

    assert window.adc_watchdog_high_edit.isHidden()
    assert label.isHidden()

    window.adc_watchdog_box.setChecked(True)
    assert not window.adc_watchdog_high_edit.isHidden()
    assert not label.isHidden()

    window.adc_watchdog_box.setChecked(False)
    assert window.adc_watchdog_high_edit.isHidden()
    assert label.isHidden()


def test_adc_list_shows_watchdog_marker(window) -> None:
    """列表行要标出看门狗窗口，否则要看详情才知道是否启用。"""
    window.on_add_adc()
    window.adc_watchdog_box.setChecked(True)
    window.adc_watchdog_high_edit.setText("3000")
    window.adc_watchdog_low_edit.setText("100")
    window._on_adc_edited()
    text = window.adc_list.item(0).text()
    assert "看门狗" in text


def test_adc_list_shows_dual_mode_marker(window) -> None:
    """双 ADC 模式要在列表里可见 —— 它要求 ADC2 也配置。"""
    window.on_add_adc()
    window.adc_mode_combo.setCurrentIndex(
        window.adc_mode_combo.findData("ADC_Mode_RegSimult")
    )
    window._on_adc_edited()
    assert "双 ADC 模式" in window.adc_list.item(0).text()


def test_adc_sync_nvic_adds_one_shared_channel(window) -> None:
    """ADC1 与 ADC2 共用 ADC1_2_IRQn，同步时只能补一条通道。

    补两条会撞上 ``nvic.duplicate_channel`` —— 那是硬件的样子，不该造重复。
    """
    window.on_add_adc()
    window.adc_interrupt_boxes["ADC_IT_EOC"].setChecked(True)
    window._on_adc_edited()

    window.on_add_adc()
    window.adc_interrupt_boxes["ADC_IT_EOC"].setChecked(True)
    window._on_adc_edited()

    window.on_sync_adc_nvic()
    channels = [
        window.nvic_table.cellWidget(row, 0).currentData()
        for row in range(window.nvic_table.rowCount())
    ]
    assert channels.count("ADC1_2_IRQn") == 1


def test_adc_load_config_fills_form(window) -> None:
    """打开配置时 ADC 页要能完整回填。"""
    from stm32gen.models import (
        AdcChannelConfig,
        AdcConfig,
        Stm32Config,
    )

    config = Stm32Config(
        project_name="adc_ui",
        adc=[
            AdcConfig(
                instance="ADC1",
                continuous=True,
                channels=[
                    AdcChannelConfig(
                        channel="ADC_Channel_3", port="GPIOA", pin=3, label="电位器"
                    )
                ],
                label="主采样",
            )
        ],
    )
    window.load_config(config)
    assert window.adc_list.count() == 1
    assert window.adc_channel_table.rowCount() == 1
    assert window.adc_continuous_box.isChecked()
    assert window.adc_channel_table.item(0, 4).text() == "电位器"


def test_adc_tab_maps_to_adc_source(window) -> None:
    """ADC 配置页对应的预览文件必须是 bsp_adc.c。"""
    from stm32gen_gui.main_window import CONFIG_TAB_SOURCES

    index = [i for i in range(window.tabs.count()) if window.tabs.tabText(i) == "ADC"][0]
    assert CONFIG_TAB_SOURCES[index] == "bsp_adc.c"


def test_shortcut_script_is_utf8_with_bom() -> None:
    """create-shortcut.ps1 必须存为**带 BOM** 的 UTF-8。

    PowerShell 5.1 读无 BOM 的 .ps1 会按系统 ANSI 代码页解码 —— 简中系统上
    文件里的中文全变乱码，脚本直接 parser error 跑不起来。这个坑刚踩过：
    用 write 工具保存后 BOM 丢了，双击 .cmd 只弹一堆语法错误。
    """
    root = Path(__file__).parent.parent
    ps1 = root / "create-shortcut.ps1"
    assert ps1.is_file(), f"找不到 {ps1}"
    raw = ps1.read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf", "create-shortcut.ps1 缺少 UTF-8 BOM"
    # 内容本身也要是合法 UTF-8（BOM 之后的部分）
    raw[3:].decode("utf-8")


def test_shortcut_script_prefers_packaged_exe() -> None:
    """脚本要优先指向打包好的 exe，而不是 venv 里的 pythonw。

    exe 自包含，拷到没装 Python 的机器也能跑；pythonw 只能在开发机上用。
    """
    root = Path(__file__).parent.parent
    text = (root / "create-shortcut.ps1").read_text(encoding="utf-8-sig")
    assert "dist\\STM32InitGen\\STM32InitGen.exe" in text.replace("/", "\\"), (
        "脚本里找不到打包 exe 的路径"
    )
    # exe 的分支必须排在 pythonw 之前
    assert text.index("Test-Path $exe") < text.index("Test-Path $pythonw")


def test_shortcut_cmd_is_ascii_only() -> None:
    """create-shortcut.cmd 必须全是 ASCII。

    cmd.exe 按系统代码页读自己的内容；中文文件名/提示写在 .cmd 里，
    在非 UTF-8 代码页下会找不到文件或显示乱码。
    """
    root = Path(__file__).parent.parent
    raw = (root / "create-shortcut.cmd").read_bytes()
    raw.decode("ascii")  # 解不开就说明混进了非 ASCII
