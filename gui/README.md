# stm32-init-gen · 桌面界面（gui/）

STM32 标准外设库初始化代码生成器的**桌面界面**（PySide6）。

配套的核心库在同仓库的 [`core\`](../core) 目录。本目录只有
界面层：表单、预览、导出、编译验证按钮。**不含任何生成或校验逻辑** ——
预览调的就是核心库的 `generate()`，与 `python -m stm32gen generate`
完全同一条代码路径，所以界面上看到的内容与命令行产出的文件逐字节一致。

## 为什么拆成两个目录

PySide6 是重量级依赖（本机约 200 MB）。核心库的常见用法是脚本、CI、
批处理 —— 那些场景根本不需要 Qt。合在一起时 `pip install stm32-init-gen`
会连带装一整套 GUI 框架。

拆开后依赖方向**严格单向**：

```
stm32gen_gui  ──▶  stm32gen（核心库）
```

反向没有任何引用：核心库的 `pyproject.toml` 里没有 PySide6，`import
stm32gen_gui` 在核心代码里一次都不出现。

## 安装

装之前先把核心库装好（两个目录共用一个 venv，建在 `core\` 里）：

```powershell
# 从仓库根（stm32-init-gen-repo\）开始
cd core
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --no-cache-dir -r requirements-dev.txt
python -m pip install --no-build-isolation --no-deps -e .

cd ..\gui
..\core\.venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps -e .
```

> **本机踩坑**：pip 缓存与 build 隔离都会无限挂起，所有 `pip install`
> 都要加 `--no-cache-dir`，editable 安装还要加 `--no-build-isolation`。

## 运行

**不用命令行**：双击项目根目录的 `启动GUI.cmd`。它用 `pythonw.exe` 启动（不弹黑窗口），
路径全部相对脚本自身所在目录解析，所以在哪儿双击都行。脚本里写的是开发期的固定
venv 路径 —— 换机器或挪了虚拟环境，改脚本顶部的两行路径即可。

**桌面快捷方式**：双击 `create-shortcut.cmd`，桌面上会出现「STM32 初始化代码生成器」，
带专属图标（`src\stm32gen_gui\assets\stm32-init-gen.ico`）。

脚本按优先级选目标：**打包好的 `dist\STM32InitGen\STM32InitGen.exe` 优先**，
没有才退回 `pythonw.exe -m stm32gen_gui`。前者自包含，拷到没装 Python 的机器
也能跑；后者只在开发期有用。

快捷方式用脚本生成而不是直接提交一个 `.lnk`：`.lnk` 里存的是绝对路径，换机器或挪目录
就变成死链；脚本每次都从自身位置推导路径，永远指向当前这套安装。
`create-shortcut.cmd` 里的内容全是 ASCII —— cmd.exe 按系统代码页读自己的内容，
中文文件名写在 `.cmd` 里在非 UTF-8 代码页下会找不到文件。中文只出现在
`create-shortcut.ps1` 内部，该文件存为带 BOM 的 UTF-8，PowerShell 5.1 才能正确读取。

命令行方式：

```powershell
stm32-init-gui              # 安装后生成的入口脚本
python -m stm32gen_gui      # 等价
```

自检（检查核心库、桥接层、Qt 平台插件是否齐备）：

```powershell
python -m stm32gen_gui --selfcheck
```

自检输出只用 GBK 可编码的字符。Windows 控制台在重定向时按本地代码页编码，
一个 `↔` 之类的符号就会让整个自检抛 `UnicodeEncodeError`，用户看到的却是
「启动不了」——`tests/test_gui.py::test_selfcheck_survives_gbk_console`
用子进程把这条钉住了。

## 目录结构

```
gui\
├─ 启动GUI.cmd          双击启动（pythonw，不弹黑窗口）
├─ create-shortcut.cmd  双击在桌面生成快捷方式（带图标）
├─ create-shortcut.ps1  快捷方式的实现（UTF-8 带 BOM）
├─ src\stm32gen_gui\
│  ├─ assets\
│  │  └─ stm32-init-gen.ico  窗口与快捷方式图标（16~256 共 7 个尺寸）
│  ├─ bridge.py        表单数据 -> 配置模型（**不依赖 Qt**，可完整单测）
│  ├─ highlighter.py   C 代码语法高亮
│  ├─ main_window.py   主窗口：菜单、工具栏、八个配置页、预览、校验面板
│  ├─ app.py           入口（含 --selfcheck）
│  └─ __main__.py      允许 python -m stm32gen_gui
├─ tests\test_gui.py   179 项测试
├─ tests\conftest.py   Qt 平台与字体环境（必须在任何 PySide6 导入之前）
└─ docs\GUI-使用说明.md 完整使用说明与各页签截图
```

图标放在包内而不是项目根：它跟着包装一起分发，不依赖当前工作目录。
`pyproject.toml` 里用 `[tool.setuptools.package-data]` 把它打进 wheel ——
漏了这条，装成 wheel 后图标会静默消失（`_app_icon()` 返回空 QIcon 而不报错）。

## 打包成独立 exe

双击 `packaging\build-exe.cmd`，产物在 `dist\STM32InitGen\`。命令行等价：

```powershell
python -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\stm32initgen.spec
```

**实测体积 76.0 MB**（不裁剪是 125.7 MB，少 39.5%）。裁成 76 MB 靠三件事，
缺一条都不行：

| 手段 | 减掉 |
|---|---|
| `excludes` 排除用不到的 Qt 子模块与标准库件 | 少量（只省 Python 模块与少量 DLL） |
| 按文件名过滤 `a.binaries` 里的 DLL | **39 MB**（`opengl32sw.dll` 19.7 + Quick/Qml 12 + PDF/OpenGL 6） |
| 按路径过滤 `a.datas`/`a.binaries` 里的插件 | 3 MB（tls、多余 imageformats） |

两个坑：

1. **`excludes` 管不了 Qt 的 DLL**。PyInstaller 的 PySide6 hook 是
   `collect_dynamic_libs("PySide6")`，整个包的 DLL 一个不落抄过来，
   只有 `excludes` 时体积只掉 7.4 MB。必须在 spec 里手工过滤 `a.binaries`。
2. **插件是 DLL，归在 `a.binaries` 不在 `a.datas`**。只过滤 `datas` 的话
   `qjpeg`/`qtiff`/`qwebp` 和 `tls` 插件原样留下。
3. `distutils` 不能写进 `excludes` —— PyInstaller 自带 `hook-distutils`
   要给它注册别名，排除后直接报
   `Target module "distutils" already imported as "ExcludedModule('distutils',)"`。

**打包后什么能用、什么不能用**：

- 生成 C 代码 —— 能用。21 个 `.j2` 模板与 `symbols.json` 已作为 data 打进包，
  `--selfcheck` 的「核心资源」一步专门验证这件事
- **编译验证 —— 不能用**。它调外部的 `armclang.exe` / `armcc.exe`，
  还要 ``$env:STM32_STDLIB_ROOT\{Start,Library,User}`` 三层头文件路径，
  这两样永远不会进 exe。对方机器上装了 Keil 并设好了 `STM32_STDLIB_ROOT`
  才能用

分发时**整个 `dist\STM32InitGen\` 目录一起拷**，exe 依赖旁边的 `_internal\`。
`console=False` 所以 `--selfcheck` 看不到输出；调试时把 spec 里的
`console` 改回 `True` 重新打包。

**图标要设两处，缺一处就有一处显示默认图标**：

| 位置 | 机制 | 在哪设 |
|---|---|---|
| 窗口 / 任务栏 | 运行时 `setWindowIcon` | `main_window.py` 的 `_app_icon()` |
| exe 文件 / 快捷方式 / 资源管理器 | 打包时嵌入资源 | spec 里 `EXE(icon=...)` |

这两套机制互不相干。只设前者，exe 在资源管理器里还是 PyInstaller 的
「文件夹+箭头」；只设后者，窗口左上角是空白。
`tests/test_gui.py::test_packaging_spec_sets_exe_icon` 钉住了第二处。

## 测试

```powershell
python -m pytest -q
python -m ruff check src tests
```

测试跑在 `QT_QPA_PLATFORM=offscreen` 下，`tests/conftest.py` 在任何
PySide6 导入之前设置它，并指向系统字体目录 —— 不设平台的话
`QMessageBox` 会进入自己的事件循环等用户点击，测试**永久挂住**而不是失败。
不设字体目录的话 Qt 把所有文字宽度算成 fallback 值，`minimumSizeHint`
虚高，布局类断言测出和用户看到的完全不同的数字。

## 分层约束

**本层只做「显示」与「收集输入」**：

- 所有校验调用 `stm32gen.validate`
- 所有生成调用 `stm32gen.generator`
- 表单与模型之间的转换调用 `stm32gen_gui.bridge`

`bridge.py` 不依赖 Qt，因此在无显示器、无事件循环的环境里也能被完整覆盖 ——
`tests/test_gui.py` 里大部分断言都是针对它的。
