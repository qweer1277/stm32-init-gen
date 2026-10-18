# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包规格 —— 裁剪版。

只留 QtCore / QtGui / QtWidgets 与它们真正依赖的插件，其余 Qt 子模块全部排除。
应用用到的 Qt 能力就这些：窗口、布局、表格、下拉框、文本编辑、文件对话框、
语法高亮（QSyntaxHighlighter 在 QtGui 里）。
"""

from pathlib import Path

HERE = Path(SPECPATH)
GUI = HERE.parent
# monorepo 布局：仓库根下 core\ 与 gui\ 并列，本 spec 在 gui\packaging\ 里。
CORE = GUI.parent / "core"

# 明确排除的 Qt 子模块。PySide6 的 hook 会把被 import 的模块对应 DLL 收进来，
# 排除这些就不会带上各自的 DLL（WebEngine 一家就 150 MB 以上）。
QT_UNUSED = [
    # 浏览器内核：本项目完全不加载网页
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "PySide6.QtWebView",
    # QML / Quick：界面全部用 QWidget 实现，没有 .qml 文件
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2",
    "PySide6.QtQuickTest",
    # 3D / 图表 / 多媒体 / 音频
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtGraphs",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtSpatialAudio",
    "PySide6.QtTextToSpeech",
    # 桌面端用不到的嵌入式/网络附属
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtSerialPort",
    "PySide6.QtSensors",
    "PySide6.QtScxml",
    "PySide6.QtStateMachine",
    "PySide6.QtRemoteObjects",
    "PySide6.QtHttpServer",
    "PySide6.QtNetworkAuth",
    "PySide6.QtDBus",
    # 开发工具类
    "PySide6.QtTest",
    "PySide6.QtDesigner",
    "PySide6.QtUiTools",
    "PySide6.QtHelp",
    "PySide6.QtSql",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
]

# 标准库里用不到的大件。注意别动 jinja2 / pydantic / yaml 的依赖链。
# distutils 不能列进来：PyInstaller 自带 hook-distutils 要给它注册别名，
# 排除后 hook 会报 "Target module distutils already imported as ExcludedModule"。
STDLIB_UNUSED = [
    "tkinter",
    "unittest",
    "pydoc",
    "doctest",
    "pdb",
    "test",
    "lib2to3",
    "sqlite3",
    "multiprocessing",
    "xmlrpc",
]

a = Analysis(
    [str(GUI / "src" / "stm32gen_gui" / "__main__.py")],
    pathex=[str(GUI / "src"), str(CORE / "src")],
    binaries=[],
    datas=[
        (str(CORE / "src" / "stm32gen" / "templates"), "stm32gen/templates"),
        (
            str(CORE / "src" / "stm32gen" / "stdlib_ref" / "symbols.json"),
            "stm32gen/stdlib_ref",
        ),
        (str(GUI / "src" / "stm32gen_gui" / "assets"), "stm32gen_gui/assets"),
    ],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=QT_UNUSED + STDLIB_UNUSED,
    noarchive=False,
)

# excludes 只管 Python 模块，管不了 DLL：PyInstaller 的 PySide6 hook 是
# collect_dynamic_libs("PySide6")，也就是说整个包的 DLL 一个不落地抄过来。
# 真正减体积必须在这里动手，按文件名剔。
_DROP_DLLS = {
    # Mesa 软件 OpenGL，19.7 MB。Qt6 在 Windows 上默认走 D3D11 后端，
    # 只有既无显卡驱动也无 ANGLE 的机器才会退回它。
    "opengl32sw.dll",
    # QML / Quick 全家：界面全部 QWidget 实现，没有 .qml 文件
    "qt6quick.dll",
    "qt6qml.dll",
    "qt6qmlmodels.dll",
    "qt6qmlmeta.dll",
    "qt6qmlworkerscript.dll",
    "qt6quickcontrols2.dll",
    "qt6quicktemplates2.dll",
    "qt6quicklayouts.dll",
    "qt6virtualkeyboard.dll",
    # PDF / OpenGL 渲染
    "qt6pdf.dll",
    "qt6opengl.dll",
}

a.binaries = [
    item
    for item in a.binaries
    if Path(item[0]).name.lower() not in _DROP_DLLS
]

# 定点剥掉体积大又用不上的运行时数据：
#   1. Qt 自带的翻译文件（界面只有中文/英文，Qt 自己的字符串用英文默认值）
#   2. 用不到的插件目录
#   3. 用不到的图片格式插件 —— 界面里没有任何位图，图标是 ico（保留 qico），
#      样式可能需要 svg（保留 qsvg）。qjpeg/qtiff/qwebp 一共 2 MB 用不上。
_DROP_PREFIXES = (
    "PySide6/translations",
    "PySide6/Qt/translations",
    "PySide6/plugins/sqldrivers",
    "PySide6/plugins/multimedia",
    "PySide6/plugins/generic",
    "PySide6/plugins/tls",
    "PySide6/plugins/networkinformation",
    "PySide6/plugins/imageformats/qgif.dll",
    "PySide6/plugins/imageformats/qicns.dll",
    "PySide6/plugins/imageformats/qjpeg.dll",
    "PySide6/plugins/imageformats/qpdf.dll",
    "PySide6/plugins/imageformats/qtga.dll",
    "PySide6/plugins/imageformats/qtiff.dll",
    "PySide6/plugins/imageformats/qwbmp.dll",
    "PySide6/plugins/imageformats/qwebp.dll",
)
_DROP_PLUGIN_NAMES = ("qsqlite", "qsqlmysql", "qsqlodbc", "qsqlpsql")

def _keep(path: str) -> bool:
    """按相对路径判断这一项该不该进包里。

    必须同时作用于 binaries 与 datas：Qt 的插件是 DLL，归在 binaries；
    翻译文件归在 datas。只过滤一边的话另一边的目录会原样留下 ——
    第一版裁剪漏了 binaries，qjpeg/qtiff/qwebp 和 tls 插件全都没被删掉。
    """
    p = path.replace("\\", "/")
    if p.startswith(_DROP_PREFIXES):
        return False
    return not any(name in p for name in _DROP_PLUGIN_NAMES)


a.binaries = [item for item in a.binaries if _keep(item[0])]
a.datas = [item for item in a.datas if _keep(item[0])]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="STM32InitGen",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # 资源管理器 / 任务栏 / 快捷方式图标。不写这一项 PyInstaller 会塞进去它
    # 自己的「文件夹+箭头」，与窗口图标不一致 —— 窗口图标由 main_window
    # 在运行时 setWindowIcon 设置，与这里是两套机制，必须都给。
    icon=str(GUI / "src" / "stm32gen_gui" / "assets" / "stm32-init-gen.ico"),
    # 发布版不弹控制台。调试时改 True，就能看到 --selfcheck 的输出。
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="STM32InitGen",
)
