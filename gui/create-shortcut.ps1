# 在桌面创建「STM32 初始化代码生成器」快捷方式。
#
# 为什么用脚本而不是直接放一个 .lnk：.lnk 里存的是绝对路径，换机器或挪目录
# 就失效。这个脚本每次都从自身位置推导路径，永远指向当前这套安装。
#
# 文件名保持 ASCII：.cmd 会按系统代码页读自己的内容，中文文件名放在 .cmd
# 里在非 UTF-8 代码页下会找不到文件。中文只出现在本文件内部（已存为带 BOM
# 的 UTF-8，PowerShell 5.1 才能正确读）。
#
# 优先级：打包好的 exe > venv 里的 python。exe 自包含，拷到没装 Python 的
# 机器上也能跑；只有当还没打包时才退回开发期的 pythonw。

$ErrorActionPreference = "Stop"

$gui = $PSScriptRoot
$core = Join-Path (Split-Path -Parent $gui) "core"
$pythonw = Join-Path $core ".venv\Scripts\pythonw.exe"
$ico = Join-Path $gui "src\stm32gen_gui\assets\stm32-init-gen.ico"
$exe = Join-Path $gui "dist\STM32InitGen\STM32InitGen.exe"

$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = Join-Path $desktop "STM32 初始化代码生成器.lnk"

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($lnk)

if (Test-Path $exe) {
    # 打包后的 exe：自包含，不需要 venv，也不需要 python
    $sc.TargetPath = $exe
    $sc.Arguments = ""
    $sc.WorkingDirectory = Split-Path $exe
    $target_text = $exe
    $kind = "打包版 exe"
} elseif (Test-Path $pythonw) {
    # 还没打包：退回开发期方式（pythonw 不弹控制台窗口）
    $sc.TargetPath = $pythonw
    $sc.Arguments = "-m stm32gen_gui"
    $sc.WorkingDirectory = $gui
    $target_text = "$pythonw -m stm32gen_gui"
    $kind = "开发版（pythonw）"
} else {
    Write-Host "[错误] 既没有打包好的 exe，也找不到 pythonw.exe" -ForegroundColor Red
    Write-Host "       exe:     $exe" -ForegroundColor Yellow
    Write-Host "       pythonw: $pythonw" -ForegroundColor Yellow
    Write-Host "       二选一：先跑 packaging\build-exe.cmd，或在 $core 里建虚拟环境" -ForegroundColor Yellow
    exit 1
}

$sc.Description = "STM32 初始化代码生成器 —— 图形界面，生成标准库工程骨架"
if (Test-Path $ico) {
    $sc.IconLocation = "$ico,0"
}
$sc.Save()

Write-Host "[ OK ] 快捷方式已创建（$kind）：$lnk" -ForegroundColor Green
Write-Host "       目标：$target_text"
