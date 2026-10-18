"""主窗口。

分层约定：**本模块只做「显示」与「收集输入」**，不含任何生成或校验逻辑。
所有校验调用 ``stm32gen.validate``，所有生成调用 ``stm32gen.generator``，
表单与模型之间的转换调用 ``stm32gen_gui.bridge``。

界面结构::

    ┌─ 菜单栏：文件 / 工具 / 帮助
    ├─ 工具栏：新建 打开 保存 生成 复制
    ├──────────────────────────────────────────────
    │ 左侧配置区（QTabWidget）  │ 右侧代码预览（QTabWidget）
    │  ├ 全局（芯片/HSE/工程名） │  ├ 显示：[仅 .c ▾]（筛选不影响导出）
    │  ├ GPIO（表格）            │  ├ bsp_init.c
    │  ├ USART（表单+列表）      │  ├ bsp_rcc.c / bsp_gpio.c
    │  └ NVIC（分组+表格）       │  ├ bsp_usart.c / bsp_nvic.c
    │                           │  ├ bsp_it.c
    │                           │  └ main.c
    ├──────────────────────────────────────────────
    └─ 状态栏 + 校验问题面板（可折叠）
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QFont, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from stm32gen import __version__, default_out_dir
from stm32gen.generator import generate
from stm32gen.generator.layout import DEFAULT_LAYOUT, OutputLayout
from stm32gen.models import (
    ChipId,
    NvicPriorityGroup,
    Stm32Config,
    UsartInstance,
    make_demo_config,
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
    TimExtTriggerPrescaler,
    TimInstance,
    TimOcMode,
    TimOcPolarity,
    TimOcPreload,
    TimOutputState,
    TimRemap,
)
from stm32gen.serializers import ConfigFormatError, load, save
from stm32gen.stdlib_ref import default_index
from stm32gen.validate import has_blocking, summarize, validate

from stm32gen_gui import bridge
from stm32gen_gui.highlighter import CHighlighter

# 表格列定义： (标题, 宽度)
GPIO_COLUMNS = (
    ("端口", 90),
    ("引脚", 60),
    ("模式", 200),
    ("速度", 130),
    ("初始电平", 110),
    ("备注", 160),
)

NVIC_COLUMNS = (
    ("中断号", 170),
    ("抢占", 60),
    ("子优先级", 80),
    ("使能", 60),
    ("所属外设", 130),
)

# EXTI 表格列：(标题, 宽度)
#
# 「线号」列要放得下 ``EXTI10（10~15）``（约 156px）再加上下拉箭头，
# 170px 会截断成「EXTI10（10…」—— 曾经设成 60px，结果只剩「EX」。
EXTI_COLUMNS = (
    ("线号", 200),
    ("端口", 100),
    ("模式", 220),
    ("触发边沿", 120),
    ("备注", 160),
)

# TIM 通道子表列：(标题, 宽度)
TIM_CHANNEL_COLUMNS = (
    ("通道", 100),
    ("模式", 190),
    ("CCR", 70),
    ("极性", 100),
    ("预装载", 130),
    ("输出", 80),
    ("端口", 90),
    ("引脚", 60),
    ("备注", 120),
)

# 预览分组。顺序即下拉顺序，**第一个是默认项**。
#
# 默认只看 .c 的理由：实际要读、要手改业务逻辑的是实现文件；13 个页签
# 平铺在一起时，找 bsp_usart.c 要先在 .h/.c 交错的一排里数一遍。
# .h 里装的是声明与给业务代码用的宏（BSP_LED1_PIN、BSP_PCLK1_HZ），
# 它们**照常生成与导出**，只是默认不在预览里占页签。
PREVIEW_GROUPS = (
    ("sources", "仅 .c（实现）"),
    ("headers", "仅 .h（接口与宏）"),
    ("all", "全部"),
)

# 左侧配置页 → 右侧预览要自动定位到的 .c 文件，顺序与 ``self.tabs`` 一致。
# 配置完一个外设马上要看它生成的实现，是这一页最常见的动作；两者对齐后
# 不必再在 13 个页签里找一遍。
# 「全局」对 bsp_init.c 而不是 bsp_rcc.c：全局页改的是芯片与时钟，但用户
# 真正想看的是这些设置落到了哪个初始化函数里，bsp_init.c 正是各外设按顺序
# 初始化的入口。
CONFIG_TAB_SOURCES = (
    "bsp_init.c",
    "bsp_gpio.c",
    "bsp_afio.c",
    "bsp_exti.c",
    "bsp_usart.c",
    "bsp_tim.c",
    "bsp_nvic.c",
)

# 布局下拉框的显示名。合并形态下所有段都在 bsp.c 里，配置页与预览文件
# 不再一一对应，``_focus_preview_for_config_tab`` 会因此退化成「总是选
# bsp.c」—— 那是正确的，因为那确实是全部实现所在。
LAYOUT_LABELS = {
    OutputLayout.SPLIT.value: "分文件（19 个）",
    OutputLayout.MERGED.value: "合并（3 个）",
}

# 合并布局下所有配置页都指向同一个实现文件。
MERGED_SOURCE = "bsp.c"


def _combo(options: list[tuple[str, str]], current: str) -> QComboBox:
    """构造一个下拉框，``options`` 为 ``[(值, 显示名)]``。

    表格里的枚举列直接用 ``QComboBox`` 作为单元格控件，而不是
    ``QStyledItemDelegate`` —— 列数不多，嵌控件更直观，取值也简单
    （``cellWidget(row, col).currentData()``）。

    弹层宽度按最长选项撑开：``QComboBox`` 默认让弹层与控件同宽，
    表格单元格一窄（如 EXTI 的「线号」列）就会把
    ``EXTI5 → EXTI9_5_IRQn`` 截成「E…n」，展开也还是截的。
    单元格里该窄就窄，但**展开的列表必须能读全**。
    每个条目同时挂上完整文本的 tooltip 作兜底。
    """
    combo = QComboBox()
    for value, label in options:
        combo.addItem(label, value)
        combo.setItemData(combo.count() - 1, label, Qt.ItemDataRole.ToolTipRole)
    index = combo.findData(current)
    if index >= 0:
        combo.setCurrentIndex(index)
    metrics = combo.fontMetrics()
    widest = max((metrics.horizontalAdvance(label) for _, label in options), default=0)
    combo.view().setMinimumWidth(widest + 40)
    return combo


def _app_icon() -> QIcon:
    """窗口/任务栏图标。

    图标放在包内而不是项目根，这样它跟着包一起安装，不依赖当前工作目录。
    文件缺失时返回空 QIcon 而不是抛异常 —— 少一个图标不该让界面打不开。
    """
    path = Path(__file__).parent / "assets" / "stm32-init-gen.ico"
    return QIcon(str(path)) if path.is_file() else QIcon()


class MainWindow(QMainWindow):
    """主窗口。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"STM32 初始化代码生成器 v{__version__}")
        self.setWindowIcon(_app_icon())
        self.resize(1440, 900)

        # 当前配置文件路径，供「保存」直接覆盖
        self._config_path: Path | None = None
        # 最近一次生成结果，供「复制」与预览使用
        self._files: dict[str, str] = {}
        # 最近一次生成的完整产物列表（供预览分组筛选，不随分组变化）
        self._generated: list = []

        self._build_menus()
        self._build_toolbar()
        self._build_body()
        self._build_statusbar()

        # 默认载入示例配置，让用户一打开就有东西可看
        self.load_config(make_demo_config())
        self.refresh_preview()

    # ------------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------------

    def _build_menus(self) -> None:
        menubar = self.menuBar()

        file_menu = menubar.addMenu("文件(&F)")
        self._add_action(file_menu, "新建(&N)", QKeySequence.StandardKey.New, self.on_new)
        self._add_action(file_menu, "打开(&O)…", QKeySequence.StandardKey.Open, self.on_open)
        self._add_action(file_menu, "保存(&S)", QKeySequence.StandardKey.Save, self.on_save)
        self._add_action(
            file_menu, "另存为(&A)…", QKeySequence.StandardKey.SaveAs, self.on_save_as
        )
        file_menu.addSeparator()
        self._add_action(
            file_menu, "导入示例配置", None, lambda: self.load_config(make_demo_config())
        )
        file_menu.addSeparator()
        self._add_action(file_menu, "退出(&Q)", QKeySequence.StandardKey.Quit, self.close)

        tools_menu = menubar.addMenu("工具(&T)")
        self._add_action(
            tools_menu, "生成代码(&G)", "Ctrl+G", self.on_generate
        )
        self._add_action(
            tools_menu, "校验配置(&V)", "Ctrl+R", self.on_validate
        )
        tools_menu.addSeparator()
        self._add_action(
            tools_menu, "编译验证(&B)", "Ctrl+B", self.on_compile
        )
        tools_menu.addSeparator()
        self._add_action(
            tools_menu, "导出到目录(&E)…", "Ctrl+E", self.on_export
        )
        self._add_action(
            tools_menu, "复制当前文件(&C)", "Ctrl+Shift+C", self.on_copy
        )

        help_menu = menubar.addMenu("帮助(&H)")
        self._add_action(help_menu, "关于(&A)", None, self.on_about)

    def _add_action(self, menu, text: str, shortcut, slot) -> QAction:
        """往菜单里加一个动作，并保存以便工具栏复用。"""
        action = QAction(text, self)
        if shortcut is not None:
            action.setShortcut(shortcut)
        action.triggered.connect(slot)
        menu.addAction(action)
        if not hasattr(self, "_actions"):
            self._actions: dict[str, QAction] = {}
        self._actions[text] = action
        return action

    def _build_toolbar(self) -> None:
        toolbar = self.addToolBar("主工具栏")
        toolbar.setMovable(False)
        for key in (
            "新建(&N)",
            "打开(&O)…",
            "保存(&S)",
            "生成代码(&G)",
            "校验配置(&V)",
            "编译验证(&B)",
            "导出到目录(&E)…",
        ):
            action = self._actions.get(key)
            if action is not None:
                toolbar.addAction(action)

    def _build_body(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal)
        # 两侧都不许被拖成 0：左侧折叠后配置区消失，右侧折叠后看不到生成结果，
        # 都是用户难以自行恢复的状态。
        splitter.setChildrenCollapsible(False)
        self.splitter = splitter

        # ---- 左侧：配置区 ----
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_global_tab(), "全局")
        self.tabs.addTab(self._build_gpio_tab(), "GPIO")
        self.tabs.addTab(self._build_afio_tab(), "AFIO")
        self.tabs.addTab(self._build_exti_tab(), "EXTI")
        self.tabs.addTab(self._build_usart_tab(), "USART")
        self.tabs.addTab(self._build_tim_tab(), "TIM")
        self.tabs.addTab(self._build_nvic_tab(), "NVIC")
        self.tabs.currentChanged.connect(self._on_config_tab_changed)
        splitter.addWidget(self.tabs)

        # ---- 右侧：预览区 ----
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        # 预览筛选：默认只看 .c，避免 13 个页签平铺
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("显示"))
        self.preview_filter = _combo(list(PREVIEW_GROUPS), PREVIEW_GROUPS[0][0])
        self.preview_filter.currentIndexChanged.connect(self._on_preview_filter_changed)
        filter_row.addWidget(self.preview_filter)

        # 产物形态：分文件（19 个）还是合并成 3 个。
        # 放在预览区而不是配置区，因为它是「怎么给」而不是「配什么」——
        # 两种形态出自同一份配置，切换不需要重新填表。
        filter_row.addSpacing(12)
        filter_row.addWidget(QLabel("布局"))
        self.layout_combo = _combo(
            [(item.value, LAYOUT_LABELS[item.value]) for item in OutputLayout],
            DEFAULT_LAYOUT.value,
        )
        self.layout_combo.currentIndexChanged.connect(self._on_layout_changed)
        filter_row.addWidget(self.layout_combo)

        filter_row.addStretch()
        right_layout.addLayout(filter_row)

        self.preview_tabs = QTabWidget()
        right_layout.addWidget(self.preview_tabs)

        splitter.addWidget(right)
        # 窗口变宽时把新增空间全给预览区：配置区是操作区，宽度够用就行；
        # 预览区是阅读区，越宽越好。两侧初始比例约 42% / 58%。
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([600, 824])

        # ---- 底部：校验问题面板 ----
        self.issue_panel = QPlainTextEdit()
        self.issue_panel.setReadOnly(True)
        self.issue_panel.setMaximumHeight(180)
        self.issue_panel.setPlaceholderText("校验问题将显示在这里")

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(6, 6, 6, 6)
        body_layout.addWidget(splitter, stretch=1)

        issue_group = QGroupBox("校验问题")
        issue_layout = QVBoxLayout(issue_group)
        issue_layout.addWidget(self.issue_panel)
        body_layout.addWidget(issue_group)

        self.setCentralWidget(body)

    def _build_statusbar(self) -> None:
        self.status_label = QLabel("就绪")
        self.statusBar().addWidget(self.status_label)
        self.summary_label = QLabel("")
        self.statusBar().addPermanentWidget(self.summary_label)

    # ---- 全局页 ----

    def _build_global_tab(self) -> QWidget:
        page = QWidget()
        layout = QFormLayout(page)

        self.chip_combo = _combo(bridge.chip_options(), ChipId.F103C8T6.value)
        # 换芯片会改变可用端口/引脚集，所以除了刷新预览还要重建 GPIO 表的下拉项
        self.chip_combo.currentIndexChanged.connect(self._on_chip_changed)
        layout.addRow("芯片型号", self.chip_combo)

        self.hse_edit = QLineEdit("8000000")
        self.hse_edit.editingFinished.connect(self.refresh_preview)
        # 改晶振会改变定时器时钟，TIM 页的频率回显必须跟着变 ——
        # 否则回显停留在旧时钟上，看着「没问题」，实际差了一倍多。
        self.hse_edit.editingFinished.connect(self._refresh_tim_timing_hint)
        layout.addRow("晶振 HSE (Hz)", self.hse_edit)

        self.project_edit = QLineEdit("stm32_init")
        self.project_edit.editingFinished.connect(self.refresh_preview)
        layout.addRow("工程名", self.project_edit)

        note = QLabel(
            "编译宏由以上三项推导，缺一不可，否则 stm32f10x.h 会 #error：\n"
            "  -DSTM32F10X_MD  -DUSE_STDPERIPH_DRIVER  -DHSE_VALUE=8000000"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #666;")
        layout.addRow("", note)

        return page

    # ---- GPIO 页 ----

    def _build_gpio_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        self.gpio_table = QTableWidget(0, len(GPIO_COLUMNS))
        self.gpio_table.setHorizontalHeaderLabels([title for title, _ in GPIO_COLUMNS])
        for index, (_, width) in enumerate(GPIO_COLUMNS):
            self.gpio_table.setColumnWidth(index, width)
        self.gpio_table.horizontalHeader().setSectionResizeMode(
            len(GPIO_COLUMNS) - 1, QHeaderView.ResizeMode.Stretch
        )
        self.gpio_table.verticalHeader().setVisible(False)
        layout.addWidget(self.gpio_table)

        buttons = QHBoxLayout()
        add_button = QPushButton("添加引脚")
        add_button.clicked.connect(self.on_add_gpio)
        remove_button = QPushButton("删除选中")
        remove_button.clicked.connect(self.on_remove_gpio)
        buttons.addWidget(add_button)
        buttons.addWidget(remove_button)
        buttons.addStretch()
        layout.addLayout(buttons)

        return page

    # ---- AFIO 页 ----

    def _build_afio_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        form = QFormLayout()
        self.afio_swj_combo = _combo(
            bridge.afio_swj_options(), AfioSwjRemap.NONE.value
        )
        self.afio_swj_combo.currentIndexChanged.connect(self.refresh_preview)
        form.addRow("SWJ 调试口释放", self.afio_swj_combo)
        layout.addLayout(form)

        note = QLabel(
            "调试口引脚 PA13/PA14（SWD）、PA15/PB3/PB4（JTAG）默认被调试器占用，\n"
            "不释放就不能当普通 GPIO 用。\n\n"
            "选「关闭 SWD + JTAG」会把 SWD 一起关掉，之后调试器再也连不上，\n"
            "只能靠 BOOT0 跳线或整片擦除救回来 —— 校验层会就此报错误。\n\n"
            "引脚重映射（USART / TIM）不在这里配置，在各自的外设页里选；\n"
            "生成时代码统一放在 bsp_afio.c，且排在 GPIO 初始化之前。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #666;")
        layout.addWidget(note)
        layout.addStretch()
        return page

    # ---- EXTI 页 ----

    def _build_exti_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        self.exti_table = QTableWidget(0, len(EXTI_COLUMNS))
        self.exti_table.setHorizontalHeaderLabels([t for t, _ in EXTI_COLUMNS])
        for index, (_, width) in enumerate(EXTI_COLUMNS):
            self.exti_table.setColumnWidth(index, width)
        self.exti_table.horizontalHeader().setSectionResizeMode(
            len(EXTI_COLUMNS) - 1, QHeaderView.ResizeMode.Stretch
        )
        self.exti_table.verticalHeader().setVisible(False)
        layout.addWidget(self.exti_table)

        buttons = QHBoxLayout()
        add_button = QPushButton("添加中断线")
        add_button.clicked.connect(self.on_add_exti)
        remove_button = QPushButton("删除选中")
        remove_button.clicked.connect(self.on_remove_exti)
        sync_button = QPushButton("从 EXTI 同步 NVIC")
        sync_button.setToolTip(
            "把中断模式的 EXTI 线对应的 NVIC 通道补进 NVIC 页（已存在的不会重复添加）"
        )
        sync_button.clicked.connect(self.on_sync_exti_nvic)
        buttons.addWidget(add_button)
        buttons.addWidget(remove_button)
        buttons.addWidget(sync_button)
        buttons.addStretch()
        layout.addLayout(buttons)

        note = QLabel(
            "线号即引脚号：EXTI3 只能接 Px3。同一条线只能接一个端口"
            "（PA0 与 PB0 不能同时做 EXTI0）。\n"
            "线 5~9 共用一个中断通道、10~15 共用一个 —— 生成的 ISR 会自动合并，"
            "并给出每条线的分支。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #666;")
        layout.addWidget(note)
        return page

    # ---- TIM 页 ----

    def _build_tim_tab(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)

        # 左：实例列表
        left = QVBoxLayout()
        left.addWidget(QLabel("定时器实例"))
        self.tim_list = QListWidget()
        self.tim_list.currentRowChanged.connect(self._on_tim_selected)
        left.addWidget(self.tim_list)
        list_buttons = QHBoxLayout()
        add_button = QPushButton("添加")
        add_button.clicked.connect(self.on_add_tim)
        remove_button = QPushButton("删除")
        remove_button.clicked.connect(self.on_remove_tim)
        list_buttons.addWidget(add_button)
        list_buttons.addWidget(remove_button)
        left.addLayout(list_buttons)
        layout.addLayout(left, stretch=2)

        # 右：参数表单
        right = QVBoxLayout()

        base_form = QFormLayout()
        # 保留引用：QFormLayout 的 setVisible 不会隐藏行标签，
        # _refresh_tim_clock_widgets 需要用 labelForField 一起隐藏。
        self.tim_base_form = base_form
        self.tim_instance_combo = _combo(bridge.tim_instance_options(), "TIM2")
        self.tim_instance_combo.currentIndexChanged.connect(self._on_tim_edited)
        base_form.addRow("实例", self.tim_instance_combo)

        # 时钟源。选外部时钟后 PSC/ARR 必须自己填（工具不知道外部信号频率），
        # 所以「期望更新」那一行会被禁用 —— 见 _refresh_tim_clock_widgets。
        self.tim_clock_source_combo = _combo(
            bridge.tim_clock_source_options(), TimClockSource.INTERNAL.value
        )
        self.tim_clock_source_combo.currentIndexChanged.connect(
            self._on_tim_clock_source_changed
        )
        base_form.addRow("时钟源", self.tim_clock_source_combo)

        self.tim_prescaler_edit = QLineEdit("71")
        self.tim_prescaler_edit.editingFinished.connect(self._on_tim_edited)
        base_form.addRow("预分频 PSC", self.tim_prescaler_edit)

        self.tim_period_edit = QLineEdit("999")
        self.tim_period_edit.editingFinished.connect(self._on_tim_edited)
        base_form.addRow("重装载 ARR", self.tim_period_edit)

        # ETR / TI 的附加参数。内部时钟下整组隐藏，避免摆一堆不生效的输入框
        # 让人以为配上了 —— 模型层也会拒绝内部时钟配 ETR 参数的组合。
        self.tim_etr_prescaler_combo = _combo(
            bridge.tim_etr_prescaler_options(), "TIM_ExtTRGPSC_OFF"
        )
        self.tim_etr_prescaler_combo.currentIndexChanged.connect(self._on_tim_edited)
        base_form.addRow("ETR 预分频", self.tim_etr_prescaler_combo)

        self.tim_etr_polarity_combo = _combo(
            bridge.tim_etr_polarity_options(), "TIM_ExtTRGPolarity_NonInverted"
        )
        self.tim_etr_polarity_combo.currentIndexChanged.connect(self._on_tim_edited)
        base_form.addRow("ETR 极性", self.tim_etr_polarity_combo)

        self.tim_etr_filter_edit = QLineEdit("0")
        self.tim_etr_filter_edit.setToolTip(
            "数字滤波器采样次数 0~15，0 表示不采样。"
            "输入信号有抖动时调大，代价是响应变慢"
        )
        self.tim_etr_filter_edit.editingFinished.connect(self._on_tim_edited)
        base_form.addRow("外部信号滤波", self.tim_etr_filter_edit)

        self.tim_external_clock_edit = QLineEdit()
        self.tim_external_clock_edit.setPlaceholderText(
            "Hz，可留空（只影响注释与实际频率显示，不影响生成的 PSC/ARR）"
        )
        self.tim_external_clock_edit.editingFinished.connect(self._on_tim_edited)
        base_form.addRow("外部时钟频率", self.tim_external_clock_edit)


        # 期望周期 → PSC/ARR 的自动反算。
        # 为什么要有这一行：PSC/ARR 是两次整数除法，用户想「每 20ms 一次」
        # 得自己试凑 72000 的因数分解，而不同分解的占空比分辨率能差几十倍。
        # 这里让用户填写**目标**（带单位，避免数量级写错），工具负责反算。
        timing_row = QWidget()
        timing_layout = QHBoxLayout(timing_row)
        timing_layout.setContentsMargins(0, 0, 0, 0)
        self.tim_target_edit = QLineEdit()
        self.tim_target_edit.setPlaceholderText("如 1kHz、20ms、0.5s（留空则手填 PSC/ARR）")
        self.tim_target_edit.returnPressed.connect(self.on_solve_tim_timing)
        timing_layout.addWidget(self.tim_target_edit, 1)
        solve_button = QPushButton("自动计算")
        solve_button.setToolTip("按期望周期反算 PSC/ARR，优先让 ARR 尽量大以提高占空比分辨率")
        solve_button.clicked.connect(self.on_solve_tim_timing)
        timing_layout.addWidget(solve_button)
        # 保留引用：切到外部时钟时要禁用它（工具不知道外部信号频率）
        self.tim_solve_button = solve_button
        base_form.addRow("期望更新", timing_row)

        self.tim_timing_label = QLabel()
        self.tim_timing_label.setWordWrap(True)
        self.tim_timing_label.setStyleSheet("color: #666;")
        base_form.addRow("实际频率", self.tim_timing_label)

        self.tim_counter_combo = _combo(
            bridge.tim_counter_mode_options(), TimCounterMode.UP.value
        )
        self.tim_counter_combo.currentIndexChanged.connect(self._on_tim_edited)
        base_form.addRow("计数模式", self.tim_counter_combo)

        self.tim_ckd_combo = _combo(
            bridge.tim_clock_division_options(), TimClockDivision.DIV1.value
        )
        self.tim_ckd_combo.currentIndexChanged.connect(self._on_tim_edited)
        base_form.addRow("时钟分频 CKD", self.tim_ckd_combo)

        self.tim_rcr_edit = QLineEdit("0")
        self.tim_rcr_edit.editingFinished.connect(self._on_tim_edited)
        self.tim_rcr_edit.setToolTip("重复计数器，仅高级定时器 TIM1 有效")
        base_form.addRow("重复计数 RCR", self.tim_rcr_edit)

        self.tim_remap_combo = _combo(bridge.tim_remap_options(), TimRemap.NONE.value)
        self.tim_remap_combo.currentIndexChanged.connect(self._on_tim_edited)
        base_form.addRow("引脚重映射", self.tim_remap_combo)

        self.tim_label_edit = QLineEdit()
        self.tim_label_edit.editingFinished.connect(self._on_tim_edited)
        base_form.addRow("备注", self.tim_label_edit)
        right.addLayout(base_form)

        right.addWidget(QLabel("中断"))
        # 每行两个：7 个勾选框横排要 1080px 宽，会把整个左侧面板的最小宽度
        # 顶到 1300px 以上，右侧预览区被挤到只剩一条 —— 这里改成网格折行。
        self.tim_interrupt_boxes: dict[str, QCheckBox] = {}
        interrupt_grid = QGridLayout()
        for index, (value, label) in enumerate(bridge.tim_interrupt_options()):
            box = QCheckBox(label)
            box.stateChanged.connect(self._on_tim_edited)
            self.tim_interrupt_boxes[value] = box
            interrupt_grid.addWidget(box, index // 2, index % 2)
        right.addLayout(interrupt_grid)

        right.addWidget(QLabel("输出比较通道"))
        self.tim_channel_table = QTableWidget(0, len(TIM_CHANNEL_COLUMNS))
        self.tim_channel_table.setHorizontalHeaderLabels(
            [t for t, _ in TIM_CHANNEL_COLUMNS]
        )
        for index, (_, width) in enumerate(TIM_CHANNEL_COLUMNS):
            self.tim_channel_table.setColumnWidth(index, width)
        self.tim_channel_table.verticalHeader().setVisible(False)
        right.addWidget(self.tim_channel_table)

        channel_buttons = QHBoxLayout()
        add_channel = QPushButton("添加通道")
        add_channel.clicked.connect(self.on_add_tim_channel)
        remove_channel = QPushButton("删除通道")
        remove_channel.clicked.connect(self.on_remove_tim_channel)
        channel_buttons.addWidget(add_channel)
        channel_buttons.addWidget(remove_channel)
        channel_buttons.addStretch()
        right.addLayout(channel_buttons)

        layout.addLayout(right, stretch=5)
        return page

    # ---- USART 页 ----

    def _build_usart_tab(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)

        # 左：实例列表
        left = QVBoxLayout()
        left.addWidget(QLabel("已配置的 USART"))
        self.usart_list = QListWidget()
        self.usart_list.currentRowChanged.connect(self._on_usart_selected)
        left.addWidget(self.usart_list)

        list_buttons = QHBoxLayout()
        add_button = QPushButton("添加")
        add_button.clicked.connect(self.on_add_usart)
        remove_button = QPushButton("删除")
        remove_button.clicked.connect(self.on_remove_usart)
        list_buttons.addWidget(add_button)
        list_buttons.addWidget(remove_button)
        left.addLayout(list_buttons)
        left_widget = QWidget()
        left_widget.setLayout(left)
        left_widget.setMaximumWidth(220)
        layout.addWidget(left_widget)

        # 右：参数表单
        form = QFormLayout()
        self.usart_instance_combo = _combo(bridge.usart_instance_options(), "USART1")
        self.usart_instance_combo.currentIndexChanged.connect(self._on_usart_edited)
        form.addRow("实例", self.usart_instance_combo)

        self.usart_baud_edit = QLineEdit("115200")
        self.usart_baud_edit.editingFinished.connect(self._on_usart_edited)
        form.addRow("波特率", self.usart_baud_edit)

        self.usart_word_combo = _combo(bridge.word_length_options(), "USART_WordLength_8b")
        self.usart_word_combo.currentIndexChanged.connect(self._on_usart_edited)
        form.addRow("字长", self.usart_word_combo)

        self.usart_stop_combo = _combo(bridge.stop_bits_options(), "USART_StopBits_1")
        self.usart_stop_combo.currentIndexChanged.connect(self._on_usart_edited)
        form.addRow("停止位", self.usart_stop_combo)

        self.usart_parity_combo = _combo(bridge.parity_options(), "USART_Parity_No")
        self.usart_parity_combo.currentIndexChanged.connect(self._on_usart_edited)
        form.addRow("校验", self.usart_parity_combo)

        self.usart_dir_combo = _combo(bridge.direction_options(), "RX_TX")
        self.usart_dir_combo.currentIndexChanged.connect(self._on_usart_edited)
        form.addRow("方向", self.usart_dir_combo)

        self.usart_flow_combo = _combo(
            bridge.flow_control_options(), "USART_HardwareFlowControl_None"
        )
        self.usart_flow_combo.currentIndexChanged.connect(self._on_usart_edited)
        form.addRow("硬件流控", self.usart_flow_combo)

        self.usart_remap_combo = _combo(bridge.remap_options(), "")
        self.usart_remap_combo.currentIndexChanged.connect(self._on_usart_edited)
        form.addRow("引脚重映射", self.usart_remap_combo)

        self.usart_tx_edit = QLineEdit("")
        self.usart_tx_edit.setPlaceholderText("留空用默认引脚")
        self.usart_tx_edit.editingFinished.connect(self._on_usart_edited)
        form.addRow("TX 引脚号", self.usart_tx_edit)

        self.usart_rx_edit = QLineEdit("")
        self.usart_rx_edit.setPlaceholderText("留空用默认引脚")
        self.usart_rx_edit.editingFinished.connect(self._on_usart_edited)
        form.addRow("RX 引脚号", self.usart_rx_edit)

        self.usart_label_edit = QLineEdit("")
        self.usart_label_edit.editingFinished.connect(self._on_usart_edited)
        form.addRow("备注", self.usart_label_edit)

        interrupts_group = QGroupBox("中断（勾选后需在 NVIC 页配对应通道）")
        interrupts_layout = QVBoxLayout(interrupts_group)
        self.usart_interrupt_boxes: dict[str, QCheckBox] = {}
        for value, label in bridge.interrupt_options():
            box = QCheckBox(label)
            box.stateChanged.connect(self._on_usart_edited)
            interrupts_layout.addWidget(box)
            self.usart_interrupt_boxes[value] = box
        form.addRow(interrupts_group)

        right = QWidget()
        right.setLayout(form)
        layout.addWidget(right, stretch=1)

        return page

    # ---- NVIC 页 ----

    def _build_nvic_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        group_row = QHBoxLayout()
        group_row.addWidget(QLabel("优先级分组"))
        self.nvic_group_combo = _combo(
            bridge.priority_group_options(), NvicPriorityGroup.GROUP2.value
        )
        self.nvic_group_combo.currentIndexChanged.connect(self.refresh_preview)
        group_row.addWidget(self.nvic_group_combo, stretch=1)
        layout.addLayout(group_row)

        hint = QLabel(
            "NVIC_PriorityGroupConfig 在整个固件里只能调用一次，"
            "若工程里别处也调用了，后调用的一方会覆盖前者。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666;")
        layout.addWidget(hint)

        self.nvic_table = QTableWidget(0, len(NVIC_COLUMNS))
        self.nvic_table.setHorizontalHeaderLabels([title for title, _ in NVIC_COLUMNS])
        for index, (_, width) in enumerate(NVIC_COLUMNS):
            self.nvic_table.setColumnWidth(index, width)
        self.nvic_table.horizontalHeader().setSectionResizeMode(
            len(NVIC_COLUMNS) - 1, QHeaderView.ResizeMode.Stretch
        )
        self.nvic_table.verticalHeader().setVisible(False)
        layout.addWidget(self.nvic_table)

        buttons = QHBoxLayout()
        add_button = QPushButton("添加通道")
        add_button.clicked.connect(self.on_add_nvic)
        remove_button = QPushButton("删除选中")
        remove_button.clicked.connect(self.on_remove_nvic)
        sync_usart = QPushButton("从 USART 中断同步")
        sync_usart.setToolTip(
            "为每个使能了中断的 USART 自动添加对应 NVIC 通道，省去手工填"
        )
        sync_usart.clicked.connect(self.on_sync_nvic)
        sync_tim = QPushButton("从 TIM 中断同步")
        sync_tim.setToolTip(
            "为每个勾了中断的 TIM 自动添加对应 NVIC 通道。"
            "TIM1 的新事件走 TIM1_UP_IRQn，TIM2~4 各只有一个 TIMx_IRQn"
        )
        sync_tim.clicked.connect(self.on_sync_tim_nvic)
        buttons.addWidget(add_button)
        buttons.addWidget(remove_button)
        buttons.addWidget(sync_usart)
        buttons.addWidget(sync_tim)
        buttons.addStretch()
        layout.addLayout(buttons)

        return page

    # ------------------------------------------------------------------
    # 表格操作
    # ------------------------------------------------------------------

    def on_add_gpio(self) -> None:
        """添加一行 GPIO 配置。

        端口与引脚都是下拉框：选项由**当前芯片封装**推导（见
        ``bridge.port_options`` / ``bridge.pin_options``），所以用户不可能
        选到封装上不存在的引脚 —— 那类错误在校验层也会报，但让用户根本
        选不出来更省事。
        """
        row = self.gpio_table.rowCount()
        self.gpio_table.insertRow(row)

        port_combo = _combo(self._port_options(), "GPIOA")
        self.gpio_table.setCellWidget(row, 0, port_combo)
        # 引脚下拉的选项取决于端口，必须先于它建立、并在端口变化时重建
        pin_combo = _combo(self._pin_options("GPIOA"), "0")
        self.gpio_table.setCellWidget(row, 1, pin_combo)
        port_combo.currentIndexChanged.connect(
            lambda _index, r=row: self._on_port_changed(r)
        )
        pin_combo.currentIndexChanged.connect(self.refresh_preview)

        self.gpio_table.setCellWidget(
            row, 2, _combo(bridge.mode_options(), "GPIO_Mode_Out_PP")
        )
        self.gpio_table.setCellWidget(
            row, 3, _combo(bridge.speed_options(), "GPIO_Speed_50MHz")
        )
        self.gpio_table.setCellWidget(
            row, 4, _combo(bridge.init_level_options(), "Bit_RESET")
        )
        self.gpio_table.setItem(row, 5, QTableWidgetItem(""))

        for column in (2, 3, 4):
            widget = self.gpio_table.cellWidget(row, column)
            if widget is not None:
                widget.currentIndexChanged.connect(self.refresh_preview)
        self.refresh_preview()

    def _current_chip_id(self) -> str:
        """当前选中的芯片型号（读取失败时回退默认芯片）。"""
        data = self.chip_combo.currentData() if hasattr(self, "chip_combo") else None
        return data or ChipId.F103C8T6.value

    def _port_options(self) -> list[tuple[str, str]]:
        """当前芯片可用的端口选项（带引脚数）。"""
        return bridge.port_options(self._current_chip_id())

    def _pin_options(self, port: str) -> list[tuple[str, str]]:
        """给定端口下可选的引脚选项。"""
        return bridge.pin_options(self._current_chip_id(), port)

    def _on_chip_changed(self) -> None:
        """芯片型号变了：重建 GPIO 表的端口/引脚下拉项，再刷新预览。

        顺序要紧 —— 先重建下拉项，再生成。反过来的话，预览用的还是旧芯片
        的引脚集，会生成一份与实际芯片不符的代码。
        """
        self._refresh_all_pin_options()
        self.refresh_preview()

    def _on_port_changed(self, row: int) -> None:
        """端口变了，引脚下拉的选项要跟着换。

        **已选引脚能保留就保留**：换端口时若把引脚重置成 0，用户从 PA13
        改到 PB13 就得重新选一次引脚，很烦。只有当新端口没有这个引脚号时
        才回退到第一个可用值。
        """
        port_widget = self.gpio_table.cellWidget(row, 0)
        pin_widget = self.gpio_table.cellWidget(row, 1)
        if port_widget is None or pin_widget is None:
            return

        previous = pin_widget.currentData()
        options = self._pin_options(port_widget.currentData())

        pin_widget.blockSignals(True)
        pin_widget.clear()
        for value, label in options:
            pin_widget.addItem(label, value)
        index = pin_widget.findData(previous)
        pin_widget.setCurrentIndex(index if index >= 0 else 0)
        pin_widget.blockSignals(False)

        self.refresh_preview()

    def _refresh_all_pin_options(self) -> None:
        """芯片型号变了：端口列表与引脚列表都可能失效，全部重建。

        典型场景是用户在 C8T6 / C6T6 之间切换 —— 二者封装与端口集相同，
        但将来加别的封装（如 LQFP64 带 GPIOE）时这里必须已经是对的。
        重建时保留能保留的选中值，理由同 ``_on_port_changed``。
        """
        for row in range(self.gpio_table.rowCount()):
            port_widget = self.gpio_table.cellWidget(row, 0)
            pin_widget = self.gpio_table.cellWidget(row, 1)
            if port_widget is None or pin_widget is None:
                continue

            old_port = port_widget.currentData()
            old_pin = pin_widget.currentData()

            port_widget.blockSignals(True)
            port_widget.clear()
            for value, label in self._port_options():
                port_widget.addItem(label, value)
            index = port_widget.findData(old_port)
            port_widget.setCurrentIndex(index if index >= 0 else 0)
            port_widget.blockSignals(False)

            pin_widget.blockSignals(True)
            pin_widget.clear()
            for value, label in self._pin_options(port_widget.currentData()):
                pin_widget.addItem(label, value)
            index = pin_widget.findData(old_pin)
            pin_widget.setCurrentIndex(index if index >= 0 else 0)
            pin_widget.blockSignals(False)

    def on_remove_gpio(self) -> None:
        row = self.gpio_table.currentRow()
        if row >= 0:
            self.gpio_table.removeRow(row)
            self.refresh_preview()

    def on_add_nvic(self) -> None:
        row = self.nvic_table.rowCount()
        self.nvic_table.insertRow(row)
        self.nvic_table.setCellWidget(
            row, 0, _combo(self._irq_options(), "USART1_IRQn")
        )
        self.nvic_table.setItem(row, 1, QTableWidgetItem("0"))
        self.nvic_table.setItem(row, 2, QTableWidgetItem("0"))
        self.nvic_table.setItem(row, 3, QTableWidgetItem("1"))
        self.nvic_table.setItem(row, 4, QTableWidgetItem(""))
        self.refresh_preview()

    def on_remove_nvic(self) -> None:
        row = self.nvic_table.currentRow()
        if row >= 0:
            self.nvic_table.removeRow(row)
            self.refresh_preview()

    # ---- EXTI 表格操作 ----

    def on_add_exti(self) -> None:
        """添加一行 EXTI 配置。

        端口与线号都是下拉框：线号即引脚号，所以「EXTI3 只能是 Px3」
        这个硬件约束从选的那一刻就成立，用户不可能配出非法组合。
        """
        row = self.exti_table.rowCount()
        self.exti_table.insertRow(row)

        # 线号：0~15，标签标注所属中断通道（5~9 / 10~15 共享）
        line_combo = _combo(self._exti_line_options(), "0")
        for index in range(line_combo.count()):
            number = int(line_combo.itemData(index))
            line_combo.setItemData(
                index,
                f"线 {number} → {self._exti_channel_label(number)}",
                Qt.ItemDataRole.ToolTipRole,
            )
        self.exti_table.setCellWidget(row, 0, line_combo)
        self.exti_table.setCellWidget(row, 1, _combo(self._port_options(), "GPIOA"))
        self.exti_table.setCellWidget(
            row, 2, _combo(bridge.exti_mode_options(), ExtiMode.INTERRUPT.value)
        )
        self.exti_table.setCellWidget(
            row, 3, _combo(bridge.exti_trigger_options(), ExtiTrigger.FALLING.value)
        )
        self.exti_table.setItem(row, 4, QTableWidgetItem(""))

        for column in (0, 1, 2, 3):
            widget = self.exti_table.cellWidget(row, column)
            if widget is not None:
                widget.currentIndexChanged.connect(self.refresh_preview)
        self.refresh_preview()

    def on_remove_exti(self) -> None:
        row = self.exti_table.currentRow()
        if row >= 0:
            self.exti_table.removeRow(row)
            self.refresh_preview()

    @staticmethod
    def _exti_channel_label(number: int) -> str:
        """某条 EXTI 线对应的中断通道描述（用于 tooltip）。"""
        if number <= 4:
            return f"独占 EXTI{number}_IRQn"
        if number <= 9:
            return "EXTI9_5_IRQn（与线 5~9 共享）"
        return "EXTI15_10_IRQn（与线 10~15 共享）"

    def _exti_line_options(self) -> list[tuple[str, str]]:
        """EXTI 线号选项，标签标注共享的中断通道。

        标签刻意保持短 —— 它是表格单元格里的下拉框。完整的
        ``EXTI5 → EXTI9_5_IRQn（与 5~9 共享）`` 有 360px 宽，
        列宽再大也放不下，会截成「E…n」。所以单元格里只留
        ``EXTI5（5~9）``，完整通道名挂 tooltip（见 ``on_add_exti``）。
        """
        result: list[tuple[str, str]] = []
        for number in range(EXTI_EXTERNAL_LINE_MIN, EXTI_EXTERNAL_LINE_MAX + 1):
            if number <= 4:
                label = f"EXTI{number}"
            elif number <= 9:
                label = f"EXTI{number}（5~9）"
            else:
                label = f"EXTI{number}（10~15）"
            result.append((str(number), label))
        return result

    def on_sync_exti_nvic(self) -> None:
        """按 EXTI 的中断模式自动补齐 NVIC 通道。

        与「从 USART 中断同步」同一个思路：勾了中断却忘记配 NVIC 通道
        是最常见的手误（中断使能要两步），这个按钮按 EXTI 配置补齐。
        """
        try:
            config = self._form_to_config()
        except Exception:
            self.status_label.setText("配置有误，无法同步")
            return

        existing = {
            self.nvic_table.cellWidget(row, 0).currentData()
            for row in range(self.nvic_table.rowCount())
            if self.nvic_table.cellWidget(row, 0) is not None
        }

        added = 0
        for line in config.exti:
            if not line.needs_nvic or line.irqn in existing:
                continue
            row = self.nvic_table.rowCount()
            self.nvic_table.insertRow(row)
            self.nvic_table.setCellWidget(row, 0, _combo(self._irq_options(), line.irqn))
            self.nvic_table.setItem(row, 1, QTableWidgetItem("0"))
            self.nvic_table.setItem(row, 2, QTableWidgetItem("0"))
            self.nvic_table.setItem(row, 3, QTableWidgetItem("1"))
            self.nvic_table.setItem(row, 4, QTableWidgetItem(line.name))
            existing.add(line.irqn)
            added += 1

        # 先刷新预览（它会把状态栏改成"生成完成…"），再写同步结果 ——
        # 顺序反了的话用户看不到"新增了几个通道"这个反馈。
        self.refresh_preview()
        self.status_label.setText(
            f"同步完成：新增 {added} 个通道" if added else "无需同步，所有中断都已有通道"
        )

    def on_sync_tim_nvic(self) -> None:
        """按 TIM 的中断配置自动补齐 NVIC 通道。

        一个 TIM 实例只加**一行**：TIM1~TIM4 的更新事件各只有一个向量
        （TIM1 是 TIM1_UP_IRQn，其余是 TIMx_IRQn），勾了几个中断都进同一条
        NVIC 通道。按中断个数逐条加会插出重复行，反而让校验报冲突。
        """
        try:
            config = self._form_to_config()
        except Exception:
            self.status_label.setText("配置有误，无法同步")
            return

        existing = {
            self.nvic_table.cellWidget(row, 0).currentData()
            for row in range(self.nvic_table.rowCount())
            if self.nvic_table.cellWidget(row, 0) is not None
        }

        added = 0
        for timer in config.tim:
            if not timer.needs_nvic or timer.irqn in existing:
                continue
            row = self.nvic_table.rowCount()
            self.nvic_table.insertRow(row)
            self.nvic_table.setCellWidget(row, 0, _combo(self._irq_options(), timer.irqn))
            self.nvic_table.setItem(row, 1, QTableWidgetItem("0"))
            self.nvic_table.setItem(row, 2, QTableWidgetItem("0"))
            self.nvic_table.setItem(row, 3, QTableWidgetItem("1"))
            self.nvic_table.setItem(row, 4, QTableWidgetItem(timer.name))
            existing.add(timer.irqn)
            added += 1

        self.refresh_preview()
        self.status_label.setText(
            f"同步完成：新增 {added} 个通道" if added else "无需同步，所有中断都已有通道"
        )

    # ---- TIM 列表与通道表操作 ----

    def on_add_tim(self) -> None:
        """添加一个 TIM 实例。"""
        used = {
            self.tim_list.item(row).data(Qt.ItemDataRole.UserRole).instance
            for row in range(self.tim_list.count())
            if self.tim_list.item(row) is not None
        }
        available = [item for item in TimInstance if item.value not in used]
        if not available:
            QMessageBox.information(
                self, "无法添加", "本芯片可用的定时器（TIM1~TIM4）都已配置。"
            )
            return

        form = bridge.TimForm(instance=available[0].value)
        item = QListWidgetItem(self._tim_display(form))
        item.setData(Qt.ItemDataRole.UserRole, form)
        self.tim_list.addItem(item)
        self.tim_list.setCurrentRow(self.tim_list.count() - 1)
        self.refresh_preview()

    def on_remove_tim(self) -> None:
        row = self.tim_list.currentRow()
        if row >= 0:
            self.tim_list.takeItem(row)
            self.refresh_preview()

    @staticmethod
    def _tim_display(form: bridge.TimForm) -> str:
        """实例列表里的一行文本。"""
        text = f"{form.instance}  PSC={form.prescaler} ARR={form.period}"
        # 外部时钟必须在列表里就能看到 —— 否则 PSC/ARR 看起来和内部时钟
        # 一样，但含义完全不同（分的是外部脉冲而不是 72MHz）
        if form.uses_external_clock:
            try:
                source = TimClockSource(form.clock_source)
                text += f"  [外部时钟:{source.label}]"
            except ValueError:
                text += "  [外部时钟]"
        if form.channels:
            text += f"  [{len(form.channels)} 通道]"
        if form.label:
            text += f"  {form.label}"
        return text

    def on_add_tim_channel(self) -> None:
        """给当前 TIM 实例添加一个输出比较通道。"""
        row = self.tim_channel_table.rowCount()
        self.tim_channel_table.insertRow(row)

        # 默认选一个还没用过的通道
        used = {
            self.tim_channel_table.cellWidget(r, 0).currentData()
            for r in range(row)
            if self.tim_channel_table.cellWidget(r, 0) is not None
        }
        free = [c for c in TimChannel if c.value not in used] or list(TimChannel)

        self.tim_channel_table.setCellWidget(
            row, 0, _combo(bridge.tim_channel_options(), free[0].value)
        )
        self.tim_channel_table.setCellWidget(
            row, 1, _combo(bridge.tim_oc_mode_options(), TimOcMode.PWM1.value)
        )
        self.tim_channel_table.setItem(row, 2, QTableWidgetItem("0"))
        self.tim_channel_table.setCellWidget(
            row, 3, _combo(bridge.tim_polarity_options(), TimOcPolarity.HIGH.value)
        )
        self.tim_channel_table.setCellWidget(
            row, 4, _combo(bridge.tim_preload_options(), TimOcPreload.ENABLE.value)
        )
        self.tim_channel_table.setCellWidget(
            row, 5,
            _combo(bridge.tim_output_state_options(), TimOutputState.ENABLE.value),
        )
        # 端口/引脚留空 = 不生成 GPIO 配置（引脚由用户在别处配）
        self.tim_channel_table.setCellWidget(
            row, 6, _combo([("", "（不配）")] + self._port_options(), "")
        )
        self.tim_channel_table.setItem(row, 7, QTableWidgetItem(""))
        self.tim_channel_table.setItem(row, 8, QTableWidgetItem(""))

        for column in (0, 1, 3, 4, 5, 6):
            widget = self.tim_channel_table.cellWidget(row, column)
            if widget is not None:
                widget.currentIndexChanged.connect(self._on_tim_edited)
        self.refresh_preview()

    def on_remove_tim_channel(self) -> None:
        row = self.tim_channel_table.currentRow()
        if row >= 0:
            self.tim_channel_table.removeRow(row)
            self.refresh_preview()

    def _on_tim_selected(self, row: int) -> None:
        """切换到某个 TIM 实例：把它的参数填进表单。"""
        if row < 0:
            return
        item = self.tim_list.item(row)
        if item is None:
            return
        form = item.data(Qt.ItemDataRole.UserRole)
        if form is None:
            return
        self._fill_tim_form(form)

    def _fill_tim_form(self, form: bridge.TimForm) -> None:
        """把 TimForm 的值填进各控件（会触发信号，先阻塞）。"""
        widgets = (
            self.tim_instance_combo,
            self.tim_prescaler_edit,
            self.tim_period_edit,
            self.tim_counter_combo,
            self.tim_ckd_combo,
            self.tim_rcr_edit,
            self.tim_remap_combo,
            self.tim_label_edit,
            self.tim_clock_source_combo,
            self.tim_etr_prescaler_combo,
            self.tim_etr_polarity_combo,
            self.tim_etr_filter_edit,
            self.tim_external_clock_edit,
        )
        for widget in widgets:
            widget.blockSignals(True)
        for box in self.tim_interrupt_boxes.values():
            box.blockSignals(True)
        self.tim_channel_table.blockSignals(True)

        self._set_combo(self.tim_instance_combo, form.instance)
        self.tim_prescaler_edit.setText(form.prescaler)
        self.tim_period_edit.setText(form.period)
        self._set_combo(self.tim_clock_source_combo, form.clock_source)
        self._set_combo(self.tim_etr_prescaler_combo, form.etr_prescaler)
        self._set_combo(self.tim_etr_polarity_combo, form.etr_polarity)
        self.tim_etr_filter_edit.setText(form.etr_filter)
        self.tim_external_clock_edit.setText(form.external_clock_hz)
        self._set_combo(self.tim_counter_combo, form.counter_mode)
        self._set_combo(self.tim_ckd_combo, form.clock_division)
        self.tim_rcr_edit.setText(form.repetition_counter)
        # 重映射选项按实例过滤（TIM4 没有部分重映射）
        self._refresh_tim_remap_options(form.remap)
        self.tim_label_edit.setText(form.label)
        for value, box in self.tim_interrupt_boxes.items():
            box.setChecked(value in form.interrupts)

        # 通道表
        self.tim_channel_table.setRowCount(0)
        for channel in form.channels:
            index = self.tim_channel_table.rowCount()
            self.tim_channel_table.insertRow(index)
            self.tim_channel_table.setCellWidget(
                index, 0, _combo(bridge.tim_channel_options(), channel.channel)
            )
            self.tim_channel_table.setCellWidget(
                index, 1, _combo(bridge.tim_oc_mode_options(), channel.mode)
            )
            self.tim_channel_table.setItem(index, 2, QTableWidgetItem(channel.pulse))
            self.tim_channel_table.setCellWidget(
                index, 3, _combo(bridge.tim_polarity_options(), channel.polarity)
            )
            self.tim_channel_table.setCellWidget(
                index, 4, _combo(bridge.tim_preload_options(), channel.preload)
            )
            self.tim_channel_table.setCellWidget(
                index, 5,
                _combo(bridge.tim_output_state_options(), channel.output_state),
            )
            self.tim_channel_table.setCellWidget(
                index, 6, _combo([("", "（不配）")] + self._port_options(), channel.port)
            )
            self.tim_channel_table.setItem(index, 7, QTableWidgetItem(channel.pin))
            self.tim_channel_table.setItem(index, 8, QTableWidgetItem(channel.label))
            for column in (0, 1, 3, 4, 5, 6):
                widget = self.tim_channel_table.cellWidget(index, column)
                if widget is not None:
                    widget.currentIndexChanged.connect(self._on_tim_edited)

        for widget in widgets:
            widget.blockSignals(False)
        for box in self.tim_interrupt_boxes.values():
            box.blockSignals(False)
        self.tim_channel_table.blockSignals(False)
        # 期望输入框不随表单存储（它是计算入口，不是配置值），清空避免
        # 用户切到另一个实例后误以为那个实例也是按这个目标反算的
        self.tim_target_edit.clear()
        self._refresh_tim_clock_widgets()
        self._refresh_tim_timing_hint()

    def _refresh_tim_remap_options(self, current: str = "") -> None:
        """按当前实例重建重映射下拉项。

        TIM4 没有部分重映射，TIM2 有两种部分重映射 —— 选项随实例变化，
        给错了会在模型层被拦（``TimRemap.instance`` 不匹配）。
        """
        instance = self.tim_instance_combo.currentData() or TimInstance.TIM2.value
        combo = self.tim_remap_combo
        combo.blockSignals(True)
        combo.clear()
        for value, label in bridge.tim_remap_options(instance):
            combo.addItem(label, value)
        index = combo.findData(current)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)

    def on_solve_tim_timing(self) -> None:
        """按「期望更新」输入反算 PSC/ARR 并填回控件。

        失败时把原因写进提示标签而不是弹窗：用户往往要连着试几个值，
        弹窗会一直挡在前面。
        """
        text = self.tim_target_edit.text().strip()
        if not text:
            self.tim_timing_label.setText("请先填写期望更新频率或周期，如 1kHz、20ms")
            return

        instance = self.tim_instance_combo.currentData() or TimInstance.TIM2.value
        center_aligned = TimCounterMode(
            self.tim_counter_combo.currentData()
        ).is_center_aligned
        try:
            solution = bridge.solve_tim_timing(
                instance,
                text,
                hse_hz=self._hse_hz(),
                center_aligned=center_aligned,
            )
        except ValueError as exc:
            self.tim_timing_label.setText(f"无法计算：{exc}")
            return

        self.tim_prescaler_edit.setText(str(solution.prescaler))
        self.tim_period_edit.setText(str(solution.period))
        self._on_tim_edited()  # 会顺带刷新频率提示

    def _hse_hz(self) -> int:
        """全局页填的晶振频率；非法时退回默认 8MHz。

        必须读这一项而不是写死 8MHz：用户把晶振改成 12MHz 时定时器时钟
        会变成 108MHz，仍按 72MHz 反算出来的 PSC/ARR 差 1.5 倍 ——
        而生成的代码是对的（它从配置取时钟树），于是「算的时候一套、
        跑起来另一套」，最难查的那类错。
        """
        try:
            return bridge.parse_int(
                self.hse_edit.text(), name="晶振频率", minimum=1_000_000, maximum=25_000_000
            )
        except ValueError:
            return 8_000_000

    def _refresh_tim_timing_hint(self) -> None:
        """回显当前 PSC/ARR 对应的实际更新频率。

        反算只保证「按下按钮那一刻」是对的；之后用户可能手改 PSC 或切换
        计数模式，频率就变了。这条回显让改动后的真实频率始终可见，
        不必等到芯片上示波器才发现。

        外部时钟下不能按时钟树算 —— 频率由外部信号决定，没填频率就只能
        说明「未知」。这里刻意不回退到 TIMxCLK：那会显示一个看起来正常、
        实际差几个数量级的数字。
        """
        if self.tim_clock_source_combo.currentData():
            self.tim_timing_label.setText(
                self._external_clock_hint()
            )
            return
        hint = bridge.tim_timing_hint(
            self.tim_instance_combo.currentData() or TimInstance.TIM2.value,
            self.tim_prescaler_edit.text(),
            self.tim_period_edit.text(),
            self.tim_counter_combo.currentData() or TimCounterMode.UP.value,
            hse_hz=self._hse_hz(),
        )
        self.tim_timing_label.setText(hint)

    def _external_clock_hint(self) -> str:
        """外部时钟模式下的频率回显。

        四种外部模式的说明不同，但都要说清「频率不来自时钟树」——
        这是外部时钟最容易误解的地方。
        """
        source = TimClockSource(self.tim_clock_source_combo.currentData())
        if source.is_itr:
            return (
                f"外部时钟：{source.label} —— 频率 = 上游定时器的更新频率，"
                "与时钟树无关；上游必须同时在运行"
            )
        if source.is_etr:
            etr_div = self.tim_etr_prescaler_combo.currentData()
            divider = 1
            for value, _ in bridge.tim_etr_prescaler_options():
                if value == etr_div:
                    divider = TimExtTriggerPrescaler(value).divider
                    break
            detail = f"ETR 预分频 ÷{divider}"
        else:
            divider = 1
            detail = "TI 输入"
        text = f"外部时钟：{source.label}（{detail}） —— 频率由外部信号决定，与时钟树无关"

        raw = self.tim_external_clock_edit.text().strip()
        if not raw:
            return text + "；未填外部频率，实际更新频率未知"
        try:
            external_hz = bridge.parse_int(
                raw, name="外部时钟频率", minimum=1, maximum=100_000_000
            )
            prescaler = bridge.parse_int(
                self.tim_prescaler_edit.text(),
                name="预分频 PSC",
                minimum=0,
                maximum=0xFFFF,
            )
            period = bridge.parse_int(
                self.tim_period_edit.text(),
                name="重装载 ARR",
                minimum=0,
                maximum=0xFFFF,
            )
        except ValueError:
            return text + "；参数非法，无法推算"
        counter_hz = external_hz / divider / (prescaler + 1)
        update_hz = counter_hz / (period + 1)
        center = TimCounterMode(
            self.tim_counter_combo.currentData() or TimCounterMode.UP.value
        ).is_center_aligned
        if center:
            update_hz /= 2
        return (
            f"{text}；按 {external_hz:g}Hz 推算：计数 {counter_hz:g}Hz、"
            f"更新 {update_hz:g}Hz"
        )

    def _on_tim_clock_source_changed(self) -> None:
        """切换时钟源：显隐外部参数、启用/禁用反算。"""
        self._refresh_tim_clock_widgets()
        self._on_tim_edited()

    def _refresh_tim_clock_widgets(self) -> None:
        """按当前时钟源调整哪些控件可见/可用。

        内部时钟：外设参数整组隐藏，「期望更新」可用（工具能反算）。
        外部时钟：反过来 —— 工具不知道外部频率，反算会给错答案，
        所以禁用那一行并提示用户手填 PSC/ARR。
        """
        external = self.tim_clock_source_combo.currentData() != TimClockSource.INTERNAL.value
        for widget in (
            self.tim_etr_prescaler_combo,
            self.tim_etr_polarity_combo,
            self.tim_etr_filter_edit,
            self.tim_external_clock_edit,
        ):
            widget.setVisible(external)
            # QFormLayout 的 setVisible 只隐藏字段部件，行标签会留在原地 ——
            # 结果是「ETR 预分频」这样的标签下面空无一物，看起来像界面出错了。
            # 必须连标签一起隐藏。
            label = self.tim_base_form.labelForField(widget)
            if label is not None:
                label.setVisible(external)
        # ETR 的预分频/极性只对 ETR 模式有意义；TI 与 ITR 模式没有这两项。
        source = TimClockSource(self.tim_clock_source_combo.currentData())
        for widget in (self.tim_etr_prescaler_combo, self.tim_etr_polarity_combo):
            usable = source.is_etr
            widget.setEnabled(usable)
            label = self.tim_base_form.labelForField(widget)
            if label is not None:
                label.setEnabled(usable)
        # 滤波对 ETR 与 TI 都有意义（同一个 ICFilter 字段），ITR 没有
        filter_usable = source.is_etr or source.is_ti
        self.tim_etr_filter_edit.setEnabled(filter_usable)
        # 外部频率对三种外部模式都能填（ITR 填上游的更新频率）
        self.tim_external_clock_edit.setEnabled(external)

        self.tim_target_edit.setEnabled(not external)
        self.tim_target_edit.setPlaceholderText(
            "外部时钟下请直接填 PSC/ARR（工具不知道外部信号频率）"
            if external
            else "如 1kHz、20ms、0.5s（留空则手填 PSC/ARR）"
        )
        self.tim_solve_button.setEnabled(not external)

    def _on_tim_edited(self) -> None:
        """TIM 表单改动：写回当前列表项并刷新预览。"""
        self._refresh_tim_timing_hint()
        row = self.tim_list.currentRow()
        if row >= 0:
            item = self.tim_list.item(row)
            if item is not None:
                form = self._read_tim_form()
                item.setData(Qt.ItemDataRole.UserRole, form)
                item.setText(self._tim_display(form))
        self.refresh_preview()

    def _read_tim_form(self) -> bridge.TimForm:
        """从控件读出当前 TIM 表单值。"""
        channels: list[bridge.TimChannelRow] = []
        for row in range(self.tim_channel_table.rowCount()):
            def text_at(column: int, _row: int = row) -> str:
                cell = self.tim_channel_table.item(_row, column)
                return cell.text() if cell else ""

            def data_at(column: int, _row: int = row) -> str:
                widget = self.tim_channel_table.cellWidget(_row, column)
                return widget.currentData() if widget is not None else ""

            channels.append(
                bridge.TimChannelRow(
                    channel=data_at(0),
                    mode=data_at(1),
                    pulse=text_at(2),
                    polarity=data_at(3),
                    preload=data_at(4),
                    output_state=data_at(5),
                    port=data_at(6),
                    pin=text_at(7),
                    label=text_at(8),
                )
            )

        interrupts = [
            value for value, box in self.tim_interrupt_boxes.items() if box.isChecked()
        ]

        return bridge.TimForm(
            instance=self.tim_instance_combo.currentData(),
            prescaler=self.tim_prescaler_edit.text(),
            period=self.tim_period_edit.text(),
            counter_mode=self.tim_counter_combo.currentData(),
            clock_division=self.tim_ckd_combo.currentData(),
            repetition_counter=self.tim_rcr_edit.text(),
            interrupts=interrupts,
            remap=self.tim_remap_combo.currentData(),
            label=self.tim_label_edit.text(),
            channels=channels,
            clock_source=self.tim_clock_source_combo.currentData()
            or TimClockSource.INTERNAL.value,
            etr_prescaler=self.tim_etr_prescaler_combo.currentData()
            or "TIM_ExtTRGPSC_OFF",
            etr_polarity=self.tim_etr_polarity_combo.currentData()
            or "TIM_ExtTRGPolarity_NonInverted",
            etr_filter=self.tim_etr_filter_edit.text(),
            external_clock_hz=self.tim_external_clock_edit.text(),
        )

    def on_sync_nvic(self) -> None:
        """按 USART 的中断配置自动补齐 NVIC 通道。"""
        try:
            config = self._form_to_config()
        except Exception:
            self.status_label.setText("配置有误，无法同步")
            return

        existing = {
            self.nvic_table.cellWidget(row, 0).currentData()
            for row in range(self.nvic_table.rowCount())
            if self.nvic_table.cellWidget(row, 0) is not None
        }

        added = 0
        for usart in config.usart:
            if not usart.interrupts:
                continue
            if usart.irqn in existing:
                continue
            row = self.nvic_table.rowCount()
            self.nvic_table.insertRow(row)
            self.nvic_table.setCellWidget(
                row, 0, _combo(self._irq_options(), usart.irqn)
            )
            self.nvic_table.setItem(row, 1, QTableWidgetItem("0"))
            self.nvic_table.setItem(row, 2, QTableWidgetItem("0"))
            self.nvic_table.setItem(row, 3, QTableWidgetItem("1"))
            self.nvic_table.setItem(
                row, 4, QTableWidgetItem(usart.instance.value)
            )
            added += 1

        # 先刷新预览（它会把状态栏改成"生成完成…"），再写同步结果 ——
        # 顺序反了的话用户看不到"新增了几个通道"这个反馈。
        self.refresh_preview()
        self.status_label.setText(
            f"同步完成：新增 {added} 个通道" if added else "无需同步，所有中断都已有通道"
        )

    def _irq_options(self) -> list[tuple[str, str]]:
        """中断号下拉选项：从标准库 IRQn_Type 里取真实存在的中断号。

        只列本工具能生成配置的外设 —— USART、EXTI、TIM。范围靠前缀过滤而不是
        手写清单，这样标准库枚举里新出现的名字自动可见，也不会编造出不存在的
        IRQn（比如 ``TIM5_IRQn`` 在中容量型号上就没有）。
        """
        index = default_index()
        names = index.enum_values("IRQn_Type")
        # TIM1 是高级定时器，更新事件走 TIM1_UP_IRQn，另有 BRK/TRG_COM/CC 三个
        # 独立向量。全部列出，用户要接哪个就选哪个。
        prefixes = ("USART", "EXTI", "TIM")
        return [(name, name) for name in names if name.startswith(prefixes)]

    # ------------------------------------------------------------------
    # USART 列表 ↔ 表单
    # ------------------------------------------------------------------

    def _on_usart_selected(self, row: int) -> None:
        """列表选中项变化时，把该实例的参数填进表单。"""
        if row < 0:
            return
        item = self.usart_list.item(row)
        if item is None:
            return
        form: bridge.UsartForm = item.data(Qt.ItemDataRole.UserRole)
        if form is None:
            return
        self._fill_usart_form(form)

    def _fill_usart_form(self, form: bridge.UsartForm) -> None:
        """把 UsartForm 的值填进各控件（会触发信号，先阻塞）。"""
        widgets = (
            self.usart_instance_combo,
            self.usart_baud_edit,
            self.usart_word_combo,
            self.usart_stop_combo,
            self.usart_parity_combo,
            self.usart_dir_combo,
            self.usart_flow_combo,
            self.usart_remap_combo,
            self.usart_tx_edit,
            self.usart_rx_edit,
            self.usart_label_edit,
        )
        for widget in widgets:
            widget.blockSignals(True)
        for box in self.usart_interrupt_boxes.values():
            box.blockSignals(True)

        self._set_combo(self.usart_instance_combo, form.instance)
        self.usart_baud_edit.setText(form.baudrate)
        self._set_combo(self.usart_word_combo, form.word_length)
        self._set_combo(self.usart_stop_combo, form.stop_bits)
        self._set_combo(self.usart_parity_combo, form.parity)
        self._set_combo(self.usart_dir_combo, form.direction)
        self._set_combo(self.usart_flow_combo, form.flow_control)
        self._set_combo(self.usart_remap_combo, form.remap)
        self.usart_tx_edit.setText(form.tx_pin)
        self.usart_rx_edit.setText(form.rx_pin)
        self.usart_label_edit.setText(form.label)
        for value, box in self.usart_interrupt_boxes.items():
            box.setChecked(value in form.interrupts)

        for widget in widgets:
            widget.blockSignals(False)
        for box in self.usart_interrupt_boxes.values():
            box.blockSignals(False)

    @staticmethod
    def _set_combo(combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _read_usart_form(self) -> bridge.UsartForm:
        """从控件读出当前表单值。"""
        return bridge.UsartForm(
            instance=self.usart_instance_combo.currentData(),
            baudrate=self.usart_baud_edit.text(),
            word_length=self.usart_word_combo.currentData(),
            stop_bits=self.usart_stop_combo.currentData(),
            parity=self.usart_parity_combo.currentData(),
            direction=self.usart_dir_combo.currentData(),
            flow_control=self.usart_flow_combo.currentData(),
            interrupts=[
                value
                for value, box in self.usart_interrupt_boxes.items()
                if box.isChecked()
            ],
            remap=self.usart_remap_combo.currentData(),
            tx_pin=self.usart_tx_edit.text(),
            rx_pin=self.usart_rx_edit.text(),
            label=self.usart_label_edit.text(),
        )

    def _on_usart_edited(self) -> None:
        """表单被改动时，把值写回列表项并刷新预览。"""
        row = self.usart_list.currentRow()
        if row >= 0:
            item = self.usart_list.item(row)
            if item is not None:
                form = self._read_usart_form()
                item.setData(Qt.ItemDataRole.UserRole, form)
                item.setText(self._usart_display(form))
        self.refresh_preview()

    @staticmethod
    def _usart_display(form: bridge.UsartForm) -> str:
        """列表里显示的一行文本。"""
        text = f"{form.instance} @ {form.baudrate}"
        if form.label:
            text += f"（{form.label}）"
        return text

    def on_add_usart(self) -> None:
        """添加一个 USART 实例。"""
        used = {
            self.usart_list.item(row).data(Qt.ItemDataRole.UserRole).instance
            for row in range(self.usart_list.count())
            if self.usart_list.item(row) is not None
        }
        for candidate in UsartInstance:
            if candidate.value not in used:
                form = bridge.UsartForm(instance=candidate.value)
                break
        else:
            QMessageBox.information(
                self, "已达上限", "本芯片只支持 USART1/2/3，全部已配置。"
            )
            return

        item = QListWidgetItem(self._usart_display(form))
        item.setData(Qt.ItemDataRole.UserRole, form)
        self.usart_list.addItem(item)
        self.usart_list.setCurrentRow(self.usart_list.count() - 1)
        # 必须显式刷新：setCurrentRow 触发的 _on_usart_selected 只填表单
        # （且会 blockSignals 防止回环），不会触发预览重建
        self.refresh_preview()

    def on_remove_usart(self) -> None:
        row = self.usart_list.currentRow()
        if row >= 0:
            self.usart_list.takeItem(row)
            self.refresh_preview()

    # ------------------------------------------------------------------
    # 表单 ↔ 配置
    # ------------------------------------------------------------------

    def _read_form(self) -> bridge.ConfigForm:
        """从全部控件读出表单数据。"""
        gpio_rows: list[bridge.GpioRow] = []
        for row in range(self.gpio_table.rowCount()):
            port_widget = self.gpio_table.cellWidget(row, 0)
            pin_widget = self.gpio_table.cellWidget(row, 1)
            mode_widget = self.gpio_table.cellWidget(row, 2)
            speed_widget = self.gpio_table.cellWidget(row, 3)
            level_widget = self.gpio_table.cellWidget(row, 4)
            label_item = self.gpio_table.item(row, 5)
            gpio_rows.append(
                bridge.GpioRow(
                    port=port_widget.currentData() if port_widget else "GPIOA",
                    # 引脚现在是下拉框，取当前项的**引脚号**（值即 "9"）
                    pin=str(pin_widget.currentData()) if pin_widget else "0",
                    mode=mode_widget.currentData() if mode_widget else "",
                    speed=speed_widget.currentData() if speed_widget else "",
                    init_level=level_widget.currentData() if level_widget else "",
                    label=label_item.text() if label_item else "",
                )
            )

        usart_forms: list[bridge.UsartForm] = []
        for row in range(self.usart_list.count()):
            item = self.usart_list.item(row)
            if item is not None:
                form = item.data(Qt.ItemDataRole.UserRole)
                if form is not None:
                    usart_forms.append(form)

        nvic_rows: list[bridge.NvicRow] = []
        for row in range(self.nvic_table.rowCount()):
            channel_widget = self.nvic_table.cellWidget(row, 0)

            def text_at(column: int, _row: int = row) -> str:
                """读第 ``_row`` 行第 ``column`` 列的文本。

                ``_row`` 用默认参数绑定当前行号：若直接引用外层的 ``row``，
                闭包会在循环结束后才求值，所有调用都会读到最后一行
                （ruff B023 报的正是这个）。
                """
                cell = self.nvic_table.item(_row, column)
                return cell.text() if cell else ""

            nvic_rows.append(
                bridge.NvicRow(
                    irq_channel=channel_widget.currentData() if channel_widget else "",
                    preemption=text_at(1),
                    sub=text_at(2),
                    enabled=text_at(3) != "0",
                    for_peripheral=text_at(4),
                )
            )

        # EXTI 表
        exti_rows: list[bridge.ExtiRow] = []
        for row in range(self.exti_table.rowCount()):
            def exti_text(column: int, _row: int = row) -> str:
                cell = self.exti_table.item(_row, column)
                return cell.text() if cell else ""

            def exti_data(column: int, _row: int = row) -> str:
                widget = self.exti_table.cellWidget(_row, column)
                return widget.currentData() if widget is not None else ""

            exti_rows.append(
                bridge.ExtiRow(
                    line=exti_data(0),
                    port=exti_data(1),
                    mode=exti_data(2),
                    trigger=exti_data(3),
                    label=exti_text(4),
                )
            )

        # TIM 列表
        tim_forms: list[bridge.TimForm] = []
        for row in range(self.tim_list.count()):
            item = self.tim_list.item(row)
            if item is not None:
                form = item.data(Qt.ItemDataRole.UserRole)
                if form is not None:
                    tim_forms.append(form)

        return bridge.ConfigForm(
            chip=self.chip_combo.currentData(),
            hse_hz=self.hse_edit.text(),
            project_name=self.project_edit.text(),
            priority_group=self.nvic_group_combo.currentData(),
            gpio_rows=gpio_rows,
            usart_forms=usart_forms,
            nvic_rows=nvic_rows,
            afio=bridge.AfioForm(swj_remap=self.afio_swj_combo.currentData()),
            exti_rows=exti_rows,
            tim_forms=tim_forms,
        )

    def _form_to_config(self) -> Stm32Config:
        """读出表单并转成配置模型；失败时抛 ``RuntimeError``。

        调用方（生成/导出）需要的是「要么有配置要么明确失败」，
        所以这里把错误列表压成异常信息，而 ``refresh_preview`` 用的是
        宽松路径（显示错误但不抛）。
        """
        result = self._read_form().to_config()
        if result.value is None:
            raise RuntimeError(result.format_errors())
        return result.value

    def load_config(self, config: Stm32Config) -> None:
        """把配置模型填进全部控件。"""
        form = bridge.ConfigForm.from_config(config)

        self._set_combo(self.chip_combo, form.chip)
        self.hse_edit.setText(form.hse_hz)
        self.project_edit.setText(form.project_name)
        self._set_combo(self.nvic_group_combo, form.priority_group)

        # GPIO 表
        self.gpio_table.setRowCount(0)
        for row in form.gpio_rows:
            self.on_add_gpio()
            index = self.gpio_table.rowCount() - 1
            # 先设端口（触发 _on_port_changed 重建引脚下拉），再设引脚
            self._set_combo(self.gpio_table.cellWidget(index, 0), row.port)
            self._set_combo(self.gpio_table.cellWidget(index, 1), row.pin)
            self._set_combo(self.gpio_table.cellWidget(index, 2), row.mode)
            self._set_combo(self.gpio_table.cellWidget(index, 3), row.speed)
            self._set_combo(self.gpio_table.cellWidget(index, 4), row.init_level)
            self.gpio_table.item(index, 5).setText(row.label)

        # USART 列表
        self.usart_list.clear()
        for usart_form in form.usart_forms:
            item = QListWidgetItem(self._usart_display(usart_form))
            item.setData(Qt.ItemDataRole.UserRole, usart_form)
            self.usart_list.addItem(item)
        if self.usart_list.count():
            self.usart_list.setCurrentRow(0)

        # NVIC 表
        self.nvic_table.setRowCount(0)
        for nvic_row in form.nvic_rows:
            index = self.nvic_table.rowCount()
            self.nvic_table.insertRow(index)
            self.nvic_table.setCellWidget(
                index, 0, _combo(self._irq_options(), nvic_row.irq_channel)
            )
            self.nvic_table.setItem(index, 1, QTableWidgetItem(nvic_row.preemption))
            self.nvic_table.setItem(index, 2, QTableWidgetItem(nvic_row.sub))
            self.nvic_table.setItem(index, 3, QTableWidgetItem("1" if nvic_row.enabled else "0"))
            self.nvic_table.setItem(
                index, 4, QTableWidgetItem(nvic_row.for_peripheral)
            )

        # AFIO 页
        self._set_combo(self.afio_swj_combo, form.afio.swj_remap)

        # EXTI 表
        self.exti_table.setRowCount(0)
        for exti_row in form.exti_rows:
            self.on_add_exti()
            index = self.exti_table.rowCount() - 1
            self._set_combo(self.exti_table.cellWidget(index, 0), exti_row.line)
            self._set_combo(self.exti_table.cellWidget(index, 1), exti_row.port)
            self._set_combo(self.exti_table.cellWidget(index, 2), exti_row.mode)
            self._set_combo(self.exti_table.cellWidget(index, 3), exti_row.trigger)
            self.exti_table.item(index, 4).setText(exti_row.label)

        # TIM 列表
        self.tim_list.clear()
        for tim_form in form.tim_forms:
            item = QListWidgetItem(self._tim_display(tim_form))
            item.setData(Qt.ItemDataRole.UserRole, tim_form)
            self.tim_list.addItem(item)
        if self.tim_list.count():
            self.tim_list.setCurrentRow(0)
        else:
            # 没有实例时清空右侧表单，避免残留上一次的显示值
            self._fill_tim_form(bridge.TimForm())

    # ------------------------------------------------------------------
    # 预览与校验
    # ------------------------------------------------------------------

    def _on_config_tab_changed(self, index: int) -> None:
        """切换左侧配置页：重新生成，并把预览定位到该外设的 .c。

        只在**切换左侧页签**时跳，不拦用户点右侧预览页签 —— 那次点击是
        明确的阅读意图，被抢走会让人觉得控件在乱动。
        """
        self.refresh_preview()
        self._focus_preview_for_config_tab(index)

    def _focus_preview_for_config_tab(self, index: int) -> bool:
        """把预览切到该配置页对应的 .c；当前分组里没有它时不动。"""
        if not 0 <= index < len(CONFIG_TAB_SOURCES):
            return False
        if self.current_layout().is_merged:
            # 合并布局下全部实现都在 bsp.c，配置页与文件不再一一对应。
            return self._select_preview_file(MERGED_SOURCE)
        return self._select_preview_file(CONFIG_TAB_SOURCES[index])

    def _select_preview_file(self, name: str) -> bool:
        """选中名为 ``name`` 的预览页签，找不到返回 False。"""
        for position in range(self.preview_tabs.count()):
            if self.preview_tabs.tabText(position) == name:
                self.preview_tabs.setCurrentIndex(position)
                return True
        return False

    def refresh_preview(self) -> None:
        """重新生成并刷新预览、校验面板与状态栏。

        表单非法时**不弹窗**，只在状态栏与问题面板里说明 —— 用户正在输入
        的过程中字段必然暂时非法，弹窗会打断输入。
        """
        form_result = self._read_form().to_config()
        if form_result.value is None:
            self.issue_panel.setPlainText(
                "表单还有问题，暂时无法生成：\n" + form_result.format_errors()
            )
            self.summary_label.setText("表单有误")
            return

        config = form_result.value
        issues = validate(config, include_info=True)

        # 校验问题面板
        lines = [summarize(issues)]
        if issues:
            lines.append("")
            for item in issues:
                lines.append(item.format())
        self.issue_panel.setPlainText("\n".join(lines))
        self.summary_label.setText(summarize(issues))

        if has_blocking(issues):
            self.status_label.setText("存在阻断性错误，生成会被拒绝")
            self._set_preview_text(
                "// 校验存在阻断性错误，已停止生成。\n"
                "// 请查看下方「校验问题」面板。\n"
            )
            return

        # 生成
        try:
            result = generate(config, layout=self.current_layout())
        except Exception as exc:  # 生成器崩溃不该让 GUI 挂掉
            self._set_preview_text(f"// 生成失败：{exc}\n")
            self.status_label.setText("生成失败")
            return

        # 反编造自检（GUI 里也做，与 CLI 一致）
        index = default_index()
        from stm32gen.stdlib_ref import find_invented

        invented = []
        for item in result.files:
            invented.extend(find_invented(item.content, index, item.name))

        self._files = {item.name: item.content for item in result.files}
        # 留一份完整产物列表：切换预览分组时要在它上面重新筛选，
        # 而不是重新跑生成（分组切换是纯显示行为，不该触发校验与渲染）
        self._generated = list(result.files)
        self._update_preview_tabs()

        if invented:
            self.status_label.setText(
                f"生成完成，但发现 {len(invented)} 个编造符号（工具缺陷）"
            )
        else:
            self.status_label.setText(
                f"生成完成：{len(result.files)} 个文件 / {result.total_lines} 行，"
                f"引用 {len(result.symbols_used)} 个标准库符号"
            )

    def _set_preview_text(self, text: str) -> None:
        """把所有预览标签页设为同一段文字（用于错误提示）。"""
        self._files = {}
        self._generated = []
        self.preview_tabs.clear()
        editor = QPlainTextEdit(text)
        editor.setReadOnly(True)
        editor.setFont(QFont("Consolas", 10))
        self.preview_tabs.addTab(editor, "错误")

    def _preview_group(self) -> str:
        """当前选中的预览分组键。"""
        data = self.preview_filter.currentData()
        return data if data else PREVIEW_GROUPS[0][0]

    def current_layout(self) -> OutputLayout:
        """当前选中的产物形态。"""
        data = self.layout_combo.currentData()
        return OutputLayout(data) if data else DEFAULT_LAYOUT

    def _on_layout_changed(self) -> None:
        """切换产物形态：重新生成预览（两种形态出自同一份配置）。

        与分组切换不同，这里必须重跑生成 —— 合并是对渲染结果的文本变换，
        不是显示的筛选。
        """
        self.refresh_preview()
        self.status_label.setText(
            f"已切换为{self.current_layout().description}，导出将写入 "
            f"{3 if self.current_layout().is_merged else 19} 个文件"
        )

    def _filtered_files(self) -> list:
        """按当前分组筛出要显示的产物。

        分组只影响**显示**：``_files`` 始终是全部产物，导出走的也是全部产物。
        """
        group = self._preview_group()
        if group == "headers":
            return [item for item in self._generated if item.name.endswith(".h")]
        if group == "all":
            return list(self._generated)
        return [item for item in self._generated if not item.name.endswith(".h")]

    def _on_preview_filter_changed(self) -> None:
        """切换预览分组：重新筛选页签，不重新生成。"""
        self._update_preview_tabs()

    def _update_preview_tabs(self) -> None:
        """重建预览标签页。

        优先保留当前选中的文件，避免每次改动都跳回第一个标签。
        切换分组时若当前文件不在新分组里，则回退到第一个。
        """
        current = self.preview_tabs.tabText(self.preview_tabs.currentIndex())
        self.preview_tabs.clear()
        target_index = 0

        for position, item in enumerate(self._filtered_files()):
            editor = QPlainTextEdit(item.content)
            editor.setReadOnly(True)
            editor.setFont(QFont("Consolas", 10))
            editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            # 高亮器必须挂在 editor 上并保持引用，否则会被 GC 掉
            editor.highlighter = CHighlighter(editor.document())
            self.preview_tabs.addTab(editor, item.name)
            if item.name == current:
                target_index = position

        self.preview_tabs.setCurrentIndex(target_index)

    # ------------------------------------------------------------------
    # 动作
    # ------------------------------------------------------------------

    def on_new(self) -> None:
        self._config_path = None
        self.load_config(Stm32Config())
        self.status_label.setText("已新建空配置")
        self.refresh_preview()

    def on_open(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "打开配置",
            "",
            "配置文件 (*.json *.yaml *.yml);;JSON (*.json);;YAML (*.yaml *.yml);;所有文件 (*)",
        )
        if not path:
            return
        try:
            config = load(path)
        except (ConfigFormatError, FileNotFoundError, Exception) as exc:
            QMessageBox.critical(self, "打开失败", str(exc))
            return
        self._config_path = Path(path)
        self.load_config(config)
        self.status_label.setText(f"已打开 {path}")
        self.refresh_preview()
        # 打开配置后预览也对齐到当前停留的配置页，与切页签的行为一致
        self._focus_preview_for_config_tab(self.tabs.currentIndex())

    def on_save(self) -> None:
        if self._config_path is None:
            self.on_save_as()
            return
        try:
            config = self._form_to_config()
        except RuntimeError as exc:
            QMessageBox.critical(self, "配置有误", f"无法保存：\n{exc}")
            return
        save(self._config_path, config)
        self.status_label.setText(f"已保存到 {self._config_path}")

    def on_save_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "另存配置",
            str(self._config_path or "config.json"),
            "JSON (*.json);;YAML (*.yaml *.yml)",
        )
        if not path:
            return
        try:
            config = self._form_to_config()
        except RuntimeError as exc:
            QMessageBox.critical(self, "配置有误", f"无法保存：\n{exc}")
            return
        save(path, config)
        self._config_path = Path(path)
        self.status_label.setText(f"已保存到 {path}")

    def on_validate(self) -> None:
        """显式跑一次校验，把结论弹出来（表单已经在实时校验了）。"""
        try:
            config = self._form_to_config()
        except RuntimeError as exc:
            QMessageBox.warning(self, "配置有误", str(exc))
            return

        issues = validate(config, include_info=True)
        text = summarize(issues)
        if issues:
            text += "\n\n" + "\n\n".join(item.format() for item in issues)
        QMessageBox.information(self, "校验结果", text)
        self.refresh_preview()

    def on_generate(self) -> None:
        """生成并刷新预览（GUI 不做写盘，写盘交给「导出」）。"""
        self.refresh_preview()
        if self._files:
            self.status_label.setText(
                f"已生成 {len(self._files)} 个文件，可用「导出到目录」保存或用「复制当前文件」取走"
            )

    def on_export(self) -> None:
        """把生成结果写到一个目录。"""
        try:
            config = self._form_to_config()
        except RuntimeError as exc:
            QMessageBox.critical(self, "配置有误", str(exc))
            return

        issues = validate(config)
        if has_blocking(issues):
            blocking = [item for item in issues if item.severity.is_blocking]
            answer = QMessageBox.question(
                self,
                "存在阻断性错误",
                f"当前配置有 {len(blocking)} 个阻断性错误，生成的代码很可能不能用。\n"
                "仍要导出吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        # 起始目录默认指向源码树旁边的成果目录（工具与成果分开放）。
        # 用户上一次选过的目录由 Qt 自己记住，这里只给个合理起点。
        directory = QFileDialog.getExistingDirectory(
            self, "选择输出目录", default_out_dir()
        )
        if not directory:
            return

        result = generate(config, layout=self.current_layout())

        # 先探测已有文件，再决定策略。
        #
        # 注意不能写成「先 write_all(overwrite=True) 再 except FileExistsError」：
        # overwrite=True 是默认值，它**永远不会抛** FileExistsError，
        # 那段 except 是死代码（初版就犯了这个错，确认框根本不会弹）。
        # 正确做法是写之前主动检查。
        target = Path(directory)
        existing = [item.name for item in result.files if (target / item.name).exists()]

        if existing:
            answer = QMessageBox.question(
                self,
                "文件已存在",
                f"输出目录里已有 {len(existing)} 个同名文件：\n"
                + "\n".join(f"  {name}" for name in existing[:8])
                + ("\n  …" if len(existing) > 8 else "")
                + "\n\n覆盖这些文件吗？（选「否」则跳过它们，只写新文件）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            skip_existing = answer != QMessageBox.StandardButton.Yes
        else:
            skip_existing = False

        try:
            written, skipped = result.write_all(directory, skip_existing=skip_existing)
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", f"写入文件时出错：\n{exc}")
            return

        text = f"已写入 {len(written)} 个文件到\n{directory}"
        if skipped:
            text += f"\n\n跳过 {len(skipped)} 个已存在文件：\n" + "\n".join(skipped)
        QMessageBox.information(self, "导出完成", text)
        self.status_label.setText(f"已导出到 {directory}")

    def on_copy(self) -> None:
        """把当前预览标签页的内容复制到剪贴板。"""
        index = self.preview_tabs.currentIndex()
        if index < 0:
            return
        name = self.preview_tabs.tabText(index)
        content = self._files.get(name)
        if content is None:
            return
        QApplication.clipboard().setText(content)
        self.status_label.setText(f"已复制 {name} 到剪贴板")

    def on_compile(self) -> None:
        """把当前配置生成的代码交给编译器验证。

        写到**临时目录**而不是用户的输出目录：编译验证是只读性质的检查，
        不该在磁盘上留下用户没要求的文件。这样用户可以先验证再决定导出。

        编译器可能缺失。缺失时明确说明「不可用」，而不是报「失败」——
        两者对用户的意义不同（前者是环境问题，后者是代码问题）。
        """
        try:
            config = self._form_to_config()
        except RuntimeError as exc:
            QMessageBox.critical(self, "配置有误", str(exc))
            return

        from stm32gen.compile_check import check_sources, find_toolchain

        toolchain = find_toolchain()
        if toolchain is None:
            QMessageBox.information(
                self,
                "编译验证不可用",
                "未找到任何编译器，无法做编译验证。\n\n"
                "装 arm-none-eabi-gcc 可做真实目标编译；\n"
                "或设置环境变量 HOST_CC 指定宿主编译器做语法检查。",
            )
            return

        # 临时目录里生成 → 编译 → 自动清理
        import tempfile

        with tempfile.TemporaryDirectory(prefix="stm32gen_check_") as tmp:
            result = generate(config, layout=self.current_layout())
            result.write_all(tmp)
            compile_result = check_sources(config, tmp, toolchain=toolchain)

        lines = [
            f"编译器：{compile_result.toolchain.label}",
            f"         {compile_result.toolchain.version}",
        ]

        # 降级路径必须明确标注，不能让用户以为做了真实目标编译
        if not compile_result.is_native:
            lines.append("")
            lines.append(
                "注意：这是宿主 gcc 的语法检查，"
                "只能发现预处理、语法与\n架构无关的类型问题，"
                "发现不了 ARM 专属问题（内联汇编、\n寄存器类型、intrinsic 等）。"
            )

        lines.append("")
        if compile_result.execution_error:
            lines.append(f"未能执行：{compile_result.execution_error}")
        else:
            lines.append(f"结论：{compile_result.summary()}")
            if compile_result.diagnostics:
                lines.append("")
                lines.append(compile_result.format_diagnostics(limit=15))

        text = "\n".join(lines)
        self.issue_panel.setPlainText(text)

        if compile_result.execution_error:
            self.status_label.setText(f"编译验证未能执行：{compile_result.execution_error}")
            QMessageBox.warning(self, "编译验证", text)
        elif compile_result.ok:
            note = f"，{len(compile_result.warnings)} 个警告" if compile_result.warnings else ""
            kind = "编译通过" if compile_result.is_native else "语法检查通过"
            self.status_label.setText(f"{kind}{note}")
            QMessageBox.information(self, "编译验证", text)
        else:
            self.status_label.setText(
                f"编译失败：{len(compile_result.errors)} 个错误"
            )
            QMessageBox.critical(self, "编译验证未通过", text)

    def on_about(self) -> None:
        QMessageBox.about(
            self,
            "关于",
            f"STM32 标准外设库初始化代码生成器 v{__version__}\n\n"
            "目标芯片：STM32F103C8T6（含 C6T6）\n"
            "支持外设：GPIO / USART / NVIC\n"
            "生成代码只使用标准外设库，不使用 HAL/LL。\n\n"
            "所有函数、宏、枚举均从工程自带的标准库头文件实测提取，"
            "生成后会做反编造自检。",
        )


def run() -> int:
    """启动 GUI。"""
    import sys

    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()
